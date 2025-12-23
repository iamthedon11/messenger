import os
import requests
import json
import re
from datetime import datetime
from flask import Flask, request
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# Environment variables
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v24.0")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_SHEETS_CREDS = os.environ.get("GOOGLE_SHEETS_CREDS")
SHEET_NAME = os.environ.get("SHEET_NAME", "Messenger_Bot_Data")

# Initialize OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# Initialize Google Sheets
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_dict = json.loads(GOOGLE_SHEETS_CREDS)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)

# Open sheets
sheet = gc.open(SHEET_NAME)
ad_products_sheet = sheet.worksheet("Ad_Products")
conversations_sheet = sheet.worksheet("Conversations")
leads_sheet = sheet.worksheet("Leads")
handoffs_sheet = sheet.worksheet("Handoffs")


@app.route("/", methods=["GET", "POST"])
def health():
    if request.method == "GET" and request.args.get("hub.mode"):
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("Verification successful", flush=True)
            return challenge, 200
        
        return "Forbidden", 403
    
    return "OK", 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        
        return "Forbidden", 403

    if request.method == "POST":
        data = request.get_json()
        print("Webhook payload:", data, flush=True)

        if "entry" in data:
            for entry in data["entry"]:
                messaging_events = entry.get("messaging", [])
                for event in messaging_events:
                    sender_id = event["sender"]["id"]
                    
                    # Capture ad_id from Click-to-Messenger ref parameter
                    if event.get("referral"):
                        ad_id = event["referral"].get("ref", "unknown")
                        save_user_ad(sender_id, ad_id)
                        print(f"User {sender_id} came from ad: {ad_id}", flush=True)
                    
                    # Handle postback (button clicks)
                    if event.get("postback"):
                        payload = event["postback"].get("payload", "")
                        handle_postback(sender_id, payload)
                    
                    # Handle regular messages
                    if event.get("message") and "text" in event["message"]:
                        text = event["message"]["text"]
                        print(f"Message from {sender_id}: {text}", flush=True)
                        
                        # Check for handoff request
                        if is_handoff_request(text):
                            handle_handoff(sender_id, text)
                            continue
                        
                        # Extract and save lead info (phone/email)
                        extract_lead_info(sender_id, text)
                        
                        # Save user message
                        save_message(sender_id, "user", text)
                        
                        # Get conversation history + product context
                        history = get_conversation_history(sender_id)
                        ad_id = get_user_ad(sender_id)
                        products_context = get_products_by_ad(ad_id) if ad_id else ""
                        
                        # Generate AI response with context
                        reply_text = get_ai_response(text, history, products_context, sender_id)
                        
                        # Save bot reply
                        save_message(sender_id, "assistant", reply_text)
                        
                        # Send reply
                        send_message(sender_id, reply_text)

        return "EVENT_RECEIVED", 200


def save_user_ad(sender_id, ad_id):
    """Save which ad the user came from"""
    try:
        cell = conversations_sheet.find(sender_id)
        if not cell:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conversations_sheet.append_row([sender_id, ad_id, timestamp, "system", "User arrived from ad"])
            print(f"Saved user {sender_id} from ad {ad_id}", flush=True)
    except Exception as e:
        print(f"Error saving user ad: {e}", flush=True)


def get_user_ad(sender_id):
    """Get the ad_id this user came from"""
    try:
        records = conversations_sheet.get_all_records()
        for record in records:
            if str(record.get("sender_id")) == str(sender_id) and record.get("ad_id"):
                return str(record["ad_id"])
    except Exception as e:
        print(f"Error getting user ad: {e}", flush=True)
    return None


def get_products_by_ad(ad_id):
    """Get products from the ad (supports up to 5 products)"""
    try:
        records = ad_products_sheet.get_all_records()
        
        for record in records:
            if str(record.get("ad_id")) == str(ad_id):
                products_text = f"Products in this ad ({record.get('ad_type', 'album')}):\n"
                products_text += f"Summary: {record.get('product_list', '')}\n\n"
                
                # Loop through products 1-5
                for i in range(1, 6):
                    name = record.get(f"product_{i}_name", "").strip()
                    if name:  # Only include if product exists
                        price = record.get(f"product_{i}_price", "")
                        details = record.get(f"product_{i}_details", "")
                        products_text += f"{i}. {name}\n"
                        products_text += f"   Price: {price}\n"
                        products_text += f"   Details: {details}\n\n"
                
                return products_text
    except Exception as e:
        print(f"Error getting products: {e}", flush=True)
    return ""


def save_message(sender_id, role, message):
    """Save conversation to Google Sheets"""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ad_id = get_user_ad(sender_id) or ""
        conversations_sheet.append_row([sender_id, ad_id, timestamp, role, message])
        print(f"Saved {role} message for {sender_id}", flush=True)
    except Exception as e:
        print(f"Error saving message: {e}", flush=True)


def get_conversation_history(sender_id, limit=10):
    """Get last N messages for this user"""
    try:
        records = conversations_sheet.get_all_records()
        user_messages = [r for r in records if str(r.get("sender_id")) == str(sender_id)]
        
        recent = user_messages[-limit:] if len(user_messages) > limit else user_messages
        
        history = []
        for msg in recent:
            if msg["role"] in ["user", "assistant"]:
                history.append({
                    "role": msg["role"],
                    "content": msg["message"]
                })
        
        return history
    except Exception as e:
        print(f"Error getting history: {e}", flush=True)
        return []


def extract_lead_info(sender_id, text):
    """Extract and save phone/email from message"""
    try:
        # Phone patterns (Sri Lanka)
        phone_pattern = r'(?:\+94|0)?[0-9]{9,10}'
        # Email pattern
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        
        phone = re.search(phone_pattern, text)
        email = re.search(email_pattern, text)
        
        if phone or email:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ad_id = get_user_ad(sender_id) or ""
            phone_val = phone.group(0) if phone else ""
            email_val = email.group(0) if email else ""
            
            # Check if lead already exists
            leads = leads_sheet.get_all_records()
            existing = any(str(l.get("sender_id")) == str(sender_id) for l in leads)
            
            if not existing:
                leads_sheet.append_row([sender_id, ad_id, "", phone_val, email_val, timestamp, "new"])
                print(f"Captured lead: phone={phone_val}, email={email_val}", flush=True)
            else:
                # Update existing lead
                for i, lead in enumerate(leads, start=2):
                    if str(lead.get("sender_id")) == str(sender_id):
                        if phone_val:
                            leads_sheet.update_cell(i, 4, phone_val)
                        if email_val:
                            leads_sheet.update_cell(i, 5, email_val)
                        break
    except Exception as e:
        print(f"Error extracting lead info: {e}", flush=True)


def is_handoff_request(text):
    """Check if user wants to talk to human"""
    keywords = ["talk to human", "speak to person", "customer service", "support", "agent", "representative"]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)


def handle_handoff(sender_id, reason):
    """Handle handoff to human support"""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        handoffs_sheet.append_row([sender_id, timestamp, reason, "pending", ""])
        
        # Send message with button
        send_handoff_message(sender_id)
        
        print(f"Handoff requested by {sender_id}", flush=True)
    except Exception as e:
        print(f"Error handling handoff: {e}", flush=True)


def handle_postback(sender_id, payload):
    """Handle button clicks"""
    if payload == "CONNECT_HUMAN":
        handle_handoff(sender_id, "Button: Connect to human")
    elif payload == "CANCEL_HANDOFF":
        send_message(sender_id, "No problem! I'm here to help. What can I assist you with?")


def send_handoff_message(sender_id):
    """Send message with handoff confirmation and button"""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    
    payload = {
        "recipient": {"id": sender_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": "I've notified our team. Someone will get back to you shortly. Meanwhile, I'm still here to help!",
                    "buttons": [
                        {
                            "type": "postback",
                            "title": "Continue with Bot",
                            "payload": "CANCEL_HANDOFF"
                        }
                    ]
                }
            }
        }
    }
    
    requests.post(url, params=params, json=payload)


def get_ai_response(user_message, history, product_context, sender_id):
    """Get AI response with full context"""
    try:
        # Check if user has requested handoff
        handoffs = handoffs_sheet.get_all_records()
        pending_handoff = any(
            str(h.get("sender_id")) == str(sender_id) and h.get("status") == "pending" 
            for h in handoffs
        )
        
        # Build system prompt
        system_prompt = """You are a helpful sales assistant for our Facebook ads. 
Be friendly, concise, and helpful. Answer questions about products clearly.
If someone wants to buy, ask for their phone number or email.
Keep responses under 3 sentences unless detailed explanation is needed."""
        
        if product_context:
            system_prompt += f"\n\n{product_context}\n\nHelp customers with questions about these specific products."
        
        if pending_handoff:
            system_prompt += "\n\nNote: Customer has requested human support. A team member will contact them soon, but you can still assist with quick questions."
        
        # Build messages with history
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=200,
            temperature=0.7
        )
        
        return response.choices[0].message.content
    except Exception as e:
        print(f"OpenAI error: {e}", flush=True)
        return "Sorry, I'm having trouble responding. Please try again or type 'talk to human' for support."


def send_message(recipient_id, text):
    """Send text message to user"""
    if not PAGE_ACCESS_TOKEN:
        return
    
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    
    r = requests.post(url, params=params, json=payload)
    print(f"Send message status: {r.status_code}", flush=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
