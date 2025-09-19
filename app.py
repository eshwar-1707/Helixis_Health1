from flask import Flask, request, jsonify
import requests
import os
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


# ====== VERIFY WEBHOOK ======
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
        entry = data["entry"][0]["changes"][0]["value"]["messages"][0]
        sender_id = entry["from"]
        user_message = entry["text"]["body"]

        # Reset memory if user types "reset"
        if user_message.lower().strip() == "reset":
            user_conversations[sender_id].clear()
            send_message(sender_id, "✅ Memory cleared. Let's start fresh.")
            return "OK", 200

        # Detect + translate user input into English
        try:
            user_message_en = GoogleTranslator(
                source="auto", target="en").translate(user_message)
            detected_lang = GoogleTranslator(
                source="auto", target="en").source  # auto-detected language
        except Exception as e:
            print("Translation error:", e)
            user_message_en = user_message
            detected_lang = "en"

        # Append user message (in English) to history
        user_conversations[sender_id].append(
            {"role": "user", "content": user_message_en})

        # Build conversation with system + history
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(list(user_conversations[sender_id]))

        # Get Gemini response (in English)
        reply_en = get_gemini_response(messages)

        # Append assistant reply to history
        user_conversations[sender_id].append(
            {"role": "assistant", "content": reply_en})

        # Translate Gemini reply back to user’s language
        if detected_lang != "en":
            try:
                reply_user_lang = GoogleTranslator(
                    source="en", target=detected_lang).translate(reply_en)
            except Exception as e:
                print("Back-translation error:", e)
                reply_user_lang = reply_en
        else:
            reply_user_lang = reply_en

        # Send back to WhatsApp
        send_message(sender_id, reply_user_lang)

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
        if msg["role"] == "system":
            contents.append({"role": "user", "parts": [
                            {"text": msg["content"]}]})
        elif msg["role"] == "user":
            contents.append({"role": "user", "parts": [
                            {"text": msg["content"]}]})
        else:  # assistant
            contents.append({"role": "model", "parts": [
                            {"text": msg["content"]}]})

    payload = {"contents": contents}

    resp = requests.post(url, headers=headers, params=params, json=payload)
    resp_json = resp.json()

    try:
        return resp_json["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print("❌ Gemini API error:", e, resp_json)
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


# ====== DEBUG STATUS ======
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
