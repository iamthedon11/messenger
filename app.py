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

# Map page IDs to their access tokens
PAGE_MAP = {
    os.environ.get("PAGE_ID_1"): os.environ.get("PAGE_ACCESS_TOKEN_1"),
    os.environ.get("PAGE_ID_2"): os.environ.get("PAGE_ACCESS_TOKEN_2"),
    os.environ.get("PAGE_ID_3"): os.environ.get("PAGE_ACCESS_TOKEN_3"),
    os.environ.get("PAGE_ID_4"): os.environ.get("PAGE_ACCESS_TOKEN_4"),
}

def get_page_token(page_id: str) -> str:
    """Return the correct page token for this page id."""
    return PAGE_MAP.get(str(page_id))


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
                page_id = entry.get("id")  # the Page receiving this message
                page_token = get_page_token(page_id)
                print(f"Incoming event for page {page_id}", flush=True)

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
                        handle_postback(sender_id, payload, page_token)
                    
                    # Handle regular messages
                    if event.get("message") and "text" in event["message"]:
                        text = event["message"]["text"]
                        print(f"Message from {sender_id}: {text}", flush=True)
                        
                        # Check for handoff request
                        if is_handoff_request(text):
                            handle_handoff(sender_id, text, page_token)
                            continue
                        
                        # Extract and save lead info (phone/email)
                        extract_lead_info(sender_id, text)
                        
                        # Save user message
                        save_message(sender_id, "user", text)
                        
                        # Get conversation history
                        history = get_conversation_history(sender_id)
                        
                        # Get ad_id if exists
                        ad_id = get_user_ad(sender_id)
                        
                        # Get products context
                        if ad_id:
                            # User came from ad - show specific ad products
                            products_context = get_products_by_ad(ad_id)
                            print(f"Loaded products for ad {ad_id}", flush=True)
                        else:
                            # Organic user - search products based on query
                            products_context = search_products_by_query(text)
                            if products_context:
                                print(f"Found products matching query: {text}", flush=True)
                            else:
                                print("No products found, using general knowledge", flush=True)
                        
                        # Generate AI response with context
                        reply_text = get_ai_response(text, history, products_context, sender_id)
                        
                        # Save bot reply
                        save_message(sender_id, "assistant", reply_text)
                        
                        # Send reply
                        send_message(sender_id, reply_text, page_token)

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
    """Get products from specific ad (for users from Click-to-Messenger)"""
    try:
        records = ad_products_sheet.get_all_records()
        
        for record in records:
            if str(record.get("ad_id")) == str(ad_id):
                products_text = f"Products in this ad ({record.get('ad_type', 'album')}):\n"
                products_text += f"Summary: {record.get('product_list', '')}\n\n"
                
                # Loop through products 1-5
                for i in range(1, 6):
                    name = record.get(f"product_{i}_name", "").strip()
                    if name:
                        price = record.get(f"product_{i}_price", "")
                        details = record.get(f"product_{i}_details", "")
                        products_text += f"{i}. {name}\n"
                        products_text += f"   Price: {price}\n"
                        products_text += f"   Details: {details}\n\n"
                
                return products_text
    except Exception as e:
        print(f"Error getting products by ad: {e}", flush=True)
    return ""


def search_products_by_query(user_query):
    """Search products using AI to understand intent (for organic users)"""
    try:
        # Step 1: Use AI to extract search keywords from user query
        search_keywords = extract_search_keywords(user_query)
        
        if not search_keywords:
            return ""
        
        print(f"AI extracted keywords: {search_keywords}", flush=True)
        
        # Step 2: Search products in sheet
        records = ad_products_sheet.get_all_records()
        matched_products = []
        
        for record in records:
            # Search in product_list and individual product names
            searchable_text = f"{record.get('product_list', '')} "
            
            for i in range(1, 6):
                name = record.get(f"product_{i}_name", "")
                details = record.get(f"product_{i}_details", "")
                searchable_text += f"{name} {details} "
            
            # Check if any keyword matches
            searchable_text_lower = searchable_text.lower()
            if any(keyword.lower() in searchable_text_lower for keyword in search_keywords):
                # Add all products from this ad
                for i in range(1, 6):
                    name = record.get(f"product_{i}_name", "").strip()
                    if name:
                        price = record.get(f"product_{i}_price", "")
                        details = record.get(f"product_{i}_details", "")
                        matched_products.append({
                            "name": name,
                            "price": price,
                            "details": details
                        })
        
        # Step 3: Format matched products
        if matched_products:
            products_text = "Available products matching your query:\n\n"
            for idx, product in enumerate(matched_products[:5], 1):  # Limit to 5 products
                products_text += f"{idx}. {product['name']}\n"
                products_text += f"   Price: {product['price']}\n"
                products_text += f"   Details: {product['details']}\n\n"
            return products_text
        
    except Exception as e:
        print(f"Error searching products: {e}", flush=True)
    
    return ""


def extract_search_keywords(user_query):
    """Use AI to extract product search keywords from user query"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """You are a product search assistant. Extract key product-related keywords from the user's query.
Return ONLY the product keywords as a comma-separated list, nothing else.
Examples:
- "I need something to cook rice" → rice cooker
- "What can keep food fresh?" → fridge, refrigerator
- "Show me kitchen items" → kitchen appliances
- "Do you have rice cookers?" → rice cooker
- "Looking for a washing machine" → washing machine, washer
If no product intent found, return "none"."""},
                {"role": "user", "content": user_query}
            ],
            max_tokens=50,
            temperature=0.3
        )
        
        keywords_text = response.choices[0].message.content.strip()
        
        if keywords_text.lower() == "none":
            return []
        
        # Split by comma and clean
        keywords = [k.strip() for k in keywords_text.split(",")]
        return keywords
        
    except Exception as e:
        print(f"Error extracting keywords: {e}", flush=True)
        return []


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


def handle_handoff(sender_id, reason, page_token):
    """Handle handoff to human support"""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        handoffs_sheet.append_row([sender_id, timestamp, reason, "pending", ""])
        
        # Send message with button
        send_handoff_message(sender_id, page_token)
        
        print(f"Handoff requested by {sender_id}", flush=True)
    except Exception as e:
        print(f"Error handling handoff: {e}", flush=True)


def handle_postback(sender_id, payload, page_token):
    """Handle button clicks"""
    if payload == "CONNECT_HUMAN":
        handle_handoff(sender_id, "Button: Connect to human", page_token)
    elif payload == "CANCEL_HANDOFF":
        send_message(sender_id, "No problem! I'm here to help. What can I assist you with?", page_token)


def send_handoff_message(sender_id, page_token):
    """Send message with handoff confirmation and button"""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    params = {"access_token": page_token}
    
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
        
        # Build system prompt with new sales assistant role
        system_prompt = """You are a professional sales assistant for an e-commerce business handling Facebook Messenger conversations.
Your goal is to convert customer inquiries into confirmed orders using proven techniques from successful past conversations.

Your Role:
- Respond naturally in Sinhala and English based on customer's language or Singlish.
- Be friendly, helpful, and persuasive without being pushy.
- Dont send lengthy messages.

Key Behaviors:
1. Product Information: Provide clear details about price, features, availability, colors, sizes.
2. Closing Questions: Ask "order karana kamathi dha?" or "do you want to place an order?" when appropriate.
3. Handle Objections: Address concerns about price, delivery time, quality professionally.
4. Confirm Orders: When customer says "ow" or "yes", confirm details (color, size, quantity, address).
5. Payment Options: Mention COD (Cash on Delivery).

Tone:
- Conversational and warm.
- Use casual Sinhala/Singlish like "ow", "එකයි", "කමතිද".
- Match the customer's communication style.

When Product Context is Available:
Use the provided product details (name, price, description, image) to give accurate information.

Never:
- Make up product information not provided.
- Promise delivery dates without confirmation.
- Share personal opinions about products.
- Be overly formal or robotic."""
        
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


def send_message(recipient_id, text, page_token):
    """Send text message to user"""
    if not page_token:
        print("Missing PAGE token for this page", flush=True)
        return
    
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    params = {"access_token": page_token}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    
    r = requests.post(url, params=params, json=payload)
    print(f"Send message status: {r.status_code}", flush=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
