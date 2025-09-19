import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# -------------------------
# CONFIG (set these as env vars in Render)
# -------------------------
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "hackathon2025")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")  # long-lived token
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# -------------------------
# MEMORY + SUMMARIZATION
# -------------------------
# Structure:
# user_memory[user_id] = {
#   "summary": "short textual summary",
#   "messages": [ {"role":"user"/"assistant", "content": "..."} ],
#   "patient_record": { "age": "", "symptoms": [], "conditions": [], "medications": [], "allergies": [], "notes": "" }
# }
user_memory = {}
MAX_EXCHANGES = 50        # keep up to 50 raw exchanges
KEEP_RECENT = 20          # keep last 20 raw messages, summarize rest
SUMMARIZE_MODEL = "gemini-1.5-flash"   # model for summarization (via REST)
REPLY_MODEL = "gemini-1.5-flash"       # model for normal replies
SUMMARY_TRIGGER = MAX_EXCHANGES + 1

# -------------------------
# Emergency triggers (examples) - add/adjust as needed
# -------------------------
EMERGENCY_KEYWORDS = [
    "chest pain", "severe bleeding", "unconscious", "shortness of breath",
    "difficulty breathing", "severe allergic", "severe allergic reaction",
    "loss of consciousness", "not breathing", "no pulse",
    "severe burn", "stroke", "sudden weakness", "severe head injury"
]

EMERGENCY_REPLY = (
    "🚨 This sounds like a medical emergency. Please call your local emergency services immediately "
    "(e.g., 108/112 in India) or go to the nearest hospital. If possible, seek help from someone nearby now."
)

# -------------------------
# SYSTEM PROMPT (medical-only + language behavior)
# -------------------------
SYSTEM_PROMPT = (
    "You are a professional medical public-health assistant. ONLY provide information about health, symptoms, "
    "first aid, precautions, medications, and wellness. If user asks unrelated topics, politely refuse and redirect "
    "to health topics. Detect the user's language and always reply in the same language. Keep answers concise, clear, "
    "and give evidence-based general advice. Do NOT provide prescriptions or any diagnoses—advise to consult clinicians when needed."
)

# -------------------------
# Helpers: Gemini REST (simple wrappers)
# -------------------------


def call_gemini_generate(model_name, prompt_text, temperature=0.0, max_output_tokens=1024):
    """
    Call Google Generative Language API generateContent endpoint with a simple prompt.
    Returns the text response or None on failure.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    params = {"key": GEMINI_API_KEY}
    payload = {
        "temperature": temperature,
        "candidate_count": 1,
        "max_output_tokens": max_output_tokens,
        "contents": [
            {"parts": [{"text": prompt_text}]}
        ]
    }
    try:
        r = requests.post(url, params=params, json=payload, timeout=30)
        r.raise_for_status()
        j = r.json()
        return j["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print("Gemini API error:", e, getattr(e, 'response', None))
        return None

# -------------------------
# Summarize old messages and extract patient record (single call, JSON output expected)
# -------------------------


def summarize_and_extract_record(user_id, old_messages):
    """
    old_messages: list of message dicts (role, content) to summarize and extract patient record.
    The function calls Gemini once with instructions to:
      - produce a short textual summary (2-3 sentences),
      - produce a compact JSON patient record with keys: age, symptoms[], conditions[], medications[], allergies[], notes.
    Returns (summary_text, patient_record_dict) or (None, None) on failure.
    """
    # Build the raw text to summarize
    raw_text = "\n".join(
        [f"{m['role']}: {m['content']}" for m in old_messages])
    instruction = (
        f"You are a medical assistant. From the following conversation, provide a JSON object with two fields:\n"
        f"1) summary: a very short summary (1-2 sentences) of the user's medical report and context.\n"
        f"2) patient_record: a JSON object with keys: age (string), symptoms (array of short phrases), "
        f"conditions (array), medications (array), allergies (array), notes (string). Fill arrays with concise items. "
        f"Return ONLY valid JSON (no surrounding text). Conversation:\n\n{raw_text}\n\n"
        f"Important: keep values short and machine-friendly. If a field is unknown, use empty string or empty array."
    )

    resp = call_gemini_generate(
        SUMMARIZE_MODEL, instruction, temperature=0.0, max_output_tokens=700)
    if not resp:
        return None, None

    # Try to locate JSON in response
    try:
        # The model should return pure JSON; if there is stray text attempt to find first { ... }
        start = resp.find("{")
        end = resp.rfind("}") + 1
        json_text = resp[start:end]
        parsed = json.loads(json_text)
        summary = parsed.get("summary", "")
        patient_record = parsed.get("patient_record", {})
        # Normalize patient_record keys
        patient_record.setdefault("age", "")
        patient_record.setdefault("symptoms", [])
        patient_record.setdefault("conditions", [])
        patient_record.setdefault("medications", [])
        patient_record.setdefault("allergies", [])
        patient_record.setdefault("notes", "")
        return summary, patient_record
    except Exception as e:
        print("Failed parsing summary JSON:", e, "raw response:", resp)
        # fallback: use resp as summary and empty record
        return resp[:500], {"age": "", "symptoms": [], "conditions": [], "medications": [], "allergies": [], "notes": ""}

# -------------------------
# Update memory + trigger summarization when needed
# -------------------------


def update_memory_and_maybe_summarize(user_id, role, content):
    if user_id not in user_memory:
        user_memory[user_id] = {"summary": "", "messages": [], "patient_record": {
            "age": "", "symptoms": [], "conditions": [], "medications": [], "allergies": [], "notes": ""}}

    user_memory[user_id]["messages"].append({"role": role, "content": content})

    if len(user_memory[user_id]["messages"]) > MAX_EXCHANGES:
        # split old vs recent
        old_msgs = user_memory[user_id]["messages"][:-KEEP_RECENT]
        recent_msgs = user_memory[user_id]["messages"][-KEEP_RECENT:]

        # summarize & extract record
        summary, record = summarize_and_extract_record(user_id, old_msgs)
        if summary is not None:
            # append summary to existing summary (concise)
            existing_summary = user_memory[user_id].get("summary", "")
            combined_summary = (existing_summary + "\n" + summary).strip()
            user_memory[user_id]["summary"] = combined_summary
            # merge records (simple union/dedup)
            pr = user_memory[user_id].get("patient_record", {})
            for key in ["symptoms", "conditions", "medications", "allergies"]:
                pr_list = pr.get(key, []) + record.get(key, [])
                # dedupe while preserving order
                deduped = []
                for item in pr_list:
                    s = item.strip()
                    if s and s not in deduped:
                        deduped.append(s)
                pr[key] = deduped
            # update scalar fields
            if not pr.get("age") and record.get("age"):
                pr["age"] = record.get("age")
            # append notes
            notes = pr.get("notes", "") + \
                ("\n" + (record.get("notes") or "")).strip()
            pr["notes"] = notes.strip()
            user_memory[user_id]["patient_record"] = pr

            # keep only recent messages + existing summary/patient_record
            user_memory[user_id]["messages"] = recent_msgs
        else:
            # summarization failed: as a fallback, just drop oldest messages and keep recent
            user_memory[user_id]["messages"] = recent_msgs

# -------------------------
# Build the prompt for Gemini reply, include system prompt, patient_record and recent messages
# -------------------------


def build_reply_prompt(user_id):
    mem = user_memory.get(
        user_id, {"summary": "", "messages": [], "patient_record": {}})
    parts = []
    # System behavior: medical-only + language instruction
    parts.append(f"SYSTEM: {SYSTEM_PROMPT}")
    # Patient record (machine friendly) if present
    pr = mem.get("patient_record") or {}
    if pr and (pr.get("age") or pr.get("symptoms") or pr.get("conditions") or pr.get("medications") or pr.get("allergies") or pr.get("notes")):
        pr_text = "PATIENT_RECORD:\n" + json.dumps(pr, ensure_ascii=False)
        parts.append(pr_text)
    # Include short summary if exists
    if mem.get("summary"):
        parts.append("PAST_SUMMARY: " + mem["summary"])
    # Include recent messages (keep last KEEP_RECENT)
    recent = mem.get("messages", [])[-KEEP_RECENT:]
    convo_text = "\n".join(
        [f"{m['role'].upper()}: {m['content']}" for m in recent])
    if convo_text:
        parts.append("RECENT_CONVERSATION:\n" + convo_text)
    # Ask model to detect language and reply in same language
    parts.append("Instruction: Detect user's language and reply in that language. Keep the response concise and medically focused (no diagnoses, recommend seeing a clinician when needed).")
    # Join
    prompt = "\n\n".join(parts)
    return prompt

# -------------------------
# Emergency detection
# -------------------------


def is_emergency(user_message):
    text = user_message.lower()
    for kw in EMERGENCY_KEYWORDS:
        if kw in text:
            return True
    return False

# -------------------------
# Main reply generator (single Gemini call)
# -------------------------


def generate_reply(user_id, user_message):
    # update memory with user message first
    update_memory_and_maybe_summarize(user_id, "user", user_message)

    # emergency check (immediate)
    if is_emergency(user_message):
        return EMERGENCY_REPLY

    # build prompt
    prompt = build_reply_prompt(user_id)

    # call Gemini
    resp_text = call_gemini_generate(
        REPLY_MODEL, prompt, temperature=0.0, max_output_tokens=700)
    if not resp_text:
        return "⚠️ I'm having trouble reaching my reasoning engine. Please try again in a moment."

    # update memory with assistant reply
    update_memory_and_maybe_summarize(user_id, "assistant", resp_text)
    return resp_text

# -------------------------
# WhatsApp sending
# -------------------------


def send_whatsapp_message(user_id, text):
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": user_id,
        "type": "text",
        "text": {"body": text}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print("WhatsApp send error:", e, getattr(e, 'response', None))

# -------------------------
# Webhook endpoints
# -------------------------


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    data = request.get_json(silent=True)
    if not data:
        return "No payload", 400

    # process incoming messages
    try:
        entries = data.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for msg in messages:
                    user_id = msg.get("from")
                    # text only support; ignore non-text messages here
                    text_body = msg.get("text", {}).get("body", "")
                    if not text_body:
                        send_whatsapp_message(
                            user_id, "Sorry, I currently only handle text messages.")
                        continue

                    if text_body.strip().lower() == "reset":
                        user_memory[user_id] = {"summary": "", "messages": [], "patient_record": {
                            "age": "", "symptoms": [], "conditions": [], "medications": [], "allergies": [], "notes": ""}}
                        send_whatsapp_message(
                            user_id, "✅ Memory cleared. You can start a new conversation.")
                        continue

                    # Generate reply (this updates memory internally)
                    reply = generate_reply(user_id, text_body)
                    send_whatsapp_message(user_id, reply)

    except Exception as e:
        print("Webhook processing error:", e)
    return "EVENT_RECEIVED", 200


@app.route("/status", methods=["GET"])
def status():
    # return active sessions & patient records (for debugging)
    out = {}
    for uid, mem in user_memory.items():
        out[uid] = {
            "messages_stored": len(mem.get("messages", [])),
            "summary": mem.get("summary", "")[:300],
            "patient_record": mem.get("patient_record", {})
        }
    return jsonify(out)


@app.route("/")
def home():
    return "Helixis Health — WhatsApp Bot is running"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
