from flask import Flask, request, jsonify
import requests
from collections import defaultdict, deque
from deep_translator import GoogleTranslator

app = Flask(__name__)

# ====== CONFIG ======
VERIFY_TOKEN = "hackathon2025"   # your verify token
WHATSAPP_TOKEN = "EAAg0NTccUccBPdNB6DcgyonLIDeObqadZAaOKbMYEsZCoxSfsQV8CG6tf0ZBncZCg0MirPYZAcK3CKubOLG10ZAPO1SKsZBa6H6JpBJTQdL92GTxy7y36jxTOWYAYEfE81lPhshrJCDYgPlMnhSO7HV4IBuuUxfJRgBazeBYc5pBV6PHiI9HzIGlIf0aD05"
PHONE_NUMBER_ID = "822103324313430"
GEMINI_API_KEY = "AIzaSyB7JCayaGoE0MbVCv5Bv3r4E74hww1mjf0"

# ====== MEMORY ======
user_conversations = defaultdict(lambda: deque(maxlen=20))

# ====== SYSTEM PROMPT ======
SYSTEM_PROMPT = (
    "You are a helpful **medical-only AI assistant**. "
    "You only provide information related to **health, symptoms, first aid, and medical advice**. "
    "If the user asks about something unrelated (like politics, sports, coding, etc.), "
    "politely decline and redirect them back to health-related topics. "
    "Keep your answers concise, clear, and professional."
)

# ====== WHATSAPP VERIFY WEBHOOK ======


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    else:
        return "Verification failed", 403

# ====== RECEIVE & PROCESS MESSAGES ======


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    try:
        change_val = data["entry"][0]["changes"][0]["value"]

        # ✅ Check if this event actually has "messages"
        if "messages" not in change_val:
            return "No user message event", 200

        entry = change_val["messages"][0]
        sender_id = entry["from"]  # phone number
        user_message = entry["text"]["body"]

        # Reset memory
        if user_message.lower().strip() == "reset":
            user_conversations[sender_id].clear()
            send_message(sender_id, "✅ Memory cleared. Let's start fresh.")
            return "OK", 200

        # Detect language & translate to English
        detected_lang = GoogleTranslator(
            source="auto", target="en").translate(user_message)
        user_conversations[sender_id].append(
            {"role": "user", "content": detected_lang})

        # Build conversation with system + history
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(list(user_conversations[sender_id]))

        # Call Gemini
        reply_en = get_gemini_response(messages)

        # Translate back to original language
        reply_final = GoogleTranslator(
            source="en", target="auto").translate(reply_en)

        # Append assistant reply
        user_conversations[sender_id].append(
            {"role": "assistant", "content": reply_en})

        # Send to WhatsApp
        send_message(sender_id, reply_final)

    except Exception as e:
        print("❌ Error handling message:", e)

    return "OK", 200

# ====== GEMINI CALL ======


def get_gemini_response(messages):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}

    contents = []
    for msg in messages:
        role = "user" if msg["role"] != "assistant" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    payload = {"contents": contents}

    try:
        resp = requests.post(url, headers=headers,
                             params=params, json=payload, timeout=15)
        resp_json = resp.json()
        return resp_json["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print("First Gemini error:", e)
        print("Raw response:", resp.text if 'resp' in locals() else None)
        # Retry once
        try:
            resp = requests.post(url, headers=headers,
                                 params=params, json=payload, timeout=15)
            resp_json = resp.json()
            return resp_json["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e2:
            print("Retry Gemini error:", e2)
            print("Raw retry response:", resp.text if 'resp' in locals() else None)
            return "⚠ Sorry, I couldn’t process that."

# ====== SEND MESSAGE TO WHATSAPP ======


def send_message(to, text):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    requests.post(url, headers=headers, json=payload)

# ====== DEBUG STATUS ROUTE ======


@app.route("/status", methods=["GET"])
def status():
    result = {}
    for user_id, history in user_conversations.items():
        result[user_id] = {
            "messages_stored": len(history),
            "last_message": history[-1]["content"] if history else "No messages yet"
        }
    return jsonify(result)


# ====== MAIN ======
if __name__ == "__main__":
    app.run(port=5000, debug=True)
