import os
import requests
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v24.0")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)


@app.route("/", methods=["GET", "POST"])
def health():
    if request.method == "GET" and request.args.get("hub.mode"):
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        print(f"ROOT VERIFY: mode={mode}, token={token}, challenge={challenge}, VERIFY_TOKEN={VERIFY_TOKEN}", flush=True)
        
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("ROOT verification successful", flush=True)
            return challenge, 200
        
        print("ROOT verification failed", flush=True)
        return "Forbidden", 403
    
    return "OK", 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        print(f"WEBHOOK VERIFY: mode={mode}, token={token}, challenge={challenge}, VERIFY_TOKEN={VERIFY_TOKEN}", flush=True)

        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("Webhook verification successful", flush=True)
            return challenge, 200
        
        print("Webhook verification failed", flush=True)
        return "Forbidden", 403

    if request.method == "POST":
        data = request.get_json()
        print("Webhook payload:", data, flush=True)

        if "entry" in data:
            for entry in data["entry"]:
                messaging_events = entry.get("messaging", [])
                for event in messaging_events:
                    if event.get("message") and "text" in event["message"]:
                        sender_id = event["sender"]["id"]
                        text = event["message"]["text"]
                        print(f"Message from {sender_id}: {text}", flush=True)

                        # Get AI response
                        reply_text = get_ai_response(text)
                        send_message(sender_id, reply_text)

        return "EVENT_RECEIVED", 200


def get_ai_response(user_message):
    """Get response from OpenAI"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful assistant for a Facebook page. Keep responses concise and friendly."},
                {"role": "user", "content": user_message}
            ],
            max_tokens=150,
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"OpenAI error: {e}", flush=True)
        return "Sorry, I'm having trouble responding right now. Please try again."


def send_message(recipient_id: str, text: str):
    if not PAGE_ACCESS_TOKEN:
        print("PAGE_ACCESS_TOKEN missing", flush=True)
        return

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }

    r = requests.post(url, params=params, json=payload)
    print(f"Send message status: {r.status_code}, response: {r.text}", flush=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
