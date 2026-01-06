import os
import requests
import json
import re
from flask import Flask, request
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

app = Flask(__name__)

# Environment Variables
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v24.0")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_SHEETS_CREDS = os.environ.get("GOOGLE_SHEETS_CREDS")
SHEET_NAME = os.environ.get("SHEET_NAME", "Messenger_Bot_Data")

# Multi-page support
PAGE_ID_1 = os.environ.get("PAGE_ID_1")
PAGE_ACCESS_TOKEN_1 = os.environ.get("PAGE_ACCESS_TOKEN_1")
PAGE_ID_2 = os.environ.get("PAGE_ID_2")
PAGE_ACCESS_TOKEN_2 = os.environ.get("PAGE_ACCESS_TOKEN_2")
PAGE_ID_3 = os.environ.get("PAGE_ID_3")
PAGE_ACCESS_TOKEN_3 = os.environ.get("PAGE_ACCESS_TOKEN_3")

# Create page mapping
PAGE_MAP = {}
if PAGE_ID_1 and PAGE_ACCESS_TOKEN_1:
    PAGE_MAP[PAGE_ID_1] = PAGE_ACCESS_TOKEN_1
if PAGE_ID_2 and PAGE_ACCESS_TOKEN_2:
    PAGE_MAP[PAGE_ID_2] = PAGE_ACCESS_TOKEN_2
if PAGE_ID_3 and PAGE_ACCESS_TOKEN_3:
    PAGE_MAP[PAGE_ID_3] = PAGE_ACCESS_TOKEN_3

# Initialize OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# Initialize Google Sheets
def get_sheet():
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDS)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        return gc.open(SHEET_NAME)
    except Exception as e:
        print(f"Google Sheets connection error: {e}", flush=True)
        return None

@app.route("/", methods=["GET", "POST"])
def health():
    if request.method == "GET" and request.args.get("hub.mode"):
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("ROOT verification successful", flush=True)
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
            print("Webhook verification successful", flush=True)
            return challenge, 200

        return "Forbidden", 403

    if request.method == "POST":
        data = request.get_json()
        print("Webhook payload:", data, flush=True)

        if "entry" in data:
            for entry in data["entry"]:
                page_id = entry.get("id")
                page_token = PAGE_MAP.get(page_id)

                messaging_events = entry.get("messaging", [])
                for event in messaging_events:
                    sender_id = event["sender"]["id"]

                    # Handle referral (ad tracking)
                    if "referral" in event:
                        ad_id = event["referral"].get("ref")
                        handle_ad_referral(sender_id, ad_id, page_token)

                    # Handle messages
                    if event.get("message") and "text" in event["message"]:
                        text = event["message"]["text"]
                        print(f"Message from {sender_id}: {text}", flush=True)
                        handle_message(sender_id, text, page_token)

        return "EVENT_RECEIVED", 200

def handle_ad_referral(sender_id, ad_id, page_token):
    """Handle new user from Click-to-Messenger ad"""
    try:
        # Save initial referral
        save_message(sender_id, ad_id, "system", f"User arrived from ad {ad_id}")

        # Send product images at start
        send_product_images_for_ad(sender_id, ad_id, page_token)

        print(f"Ad referral: sender={sender_id}, ad_id={ad_id}", flush=True)
    except Exception as e:
        print(f"Error in handle_ad_referral: {e}", flush=True)

def handle_message(sender_id, text, page_token):
    """Main message handler"""
    try:
        # Get user's ad_id
        ad_id = get_user_ad_id(sender_id)

        # Save user message
        save_message(sender_id, ad_id, "user", text)

        # Detect language preference
        language = detect_language(text)

        # Check for order placement
        order_detected = detect_order_placement(text)

        # Extract lead info (phone, address, name)
        lead_info = extract_lead_info(text)
        if lead_info:
            save_lead(sender_id, ad_id, lead_info)

        # Get conversation history
        history = get_conversation_history(sender_id)

        # Get products and send images if found
        products_context = None
        product_images = []

        if ad_id:
            products_context, product_images = get_products_for_ad(ad_id)
        else:
            # For organic users, search products and send images
            products_context, product_images = search_products_by_query(text)

        # Send product images if found (for organic conversations)
        if product_images and not ad_id:
            for img_url in product_images[:3]:  # Send max 3 images
                send_image(sender_id, img_url, page_token)

        # Generate AI response
        reply_text = get_ai_response(text, history, products_context, language, order_detected, lead_info)

        # Save bot response
        save_message(sender_id, ad_id, "assistant", reply_text)

        # If order was placed, save to Leads with order details
        if order_detected and lead_info:
            save_order_to_leads(sender_id, ad_id, lead_info, products_context)

        # Send reply
        send_message(sender_id, reply_text, page_token)

    except Exception as e:
        print(f"Error in handle_message: {e}", flush=True)
        send_message(sender_id, "Sorry, I'm having trouble right now. Please try again. Dear 💙", page_token)

def detect_language(text):
    """Detect if user is speaking Sinhala, English, or Singlish"""
    # Check for Sinhala Unicode characters
    sinhala_pattern = re.compile('[\u0D80-\u0DFF]')
    has_sinhala = bool(sinhala_pattern.search(text))

    # Check for English words
    english_words = re.findall(r'\b[a-zA-Z]+\b', text)
    has_english = len(english_words) > 0

    if has_sinhala and has_english:
        return "singlish"
    elif has_sinhala:
        return "sinhala"
    else:
        return "english"

def detect_order_placement(text):
    """Detect if customer is placing an order"""
    order_keywords = [
        'order', 'ඕඩර්', 'ගන්නම්', 'ගන්න', 'කරන්න', 'confirm', 
        'ගන්නවා', 'ඕනා', 'ඕන', 'එකක්', 'දෙන්න', 'යවන්න'
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in order_keywords)

def extract_lead_info(text):
    """Extract name, phone, address from message"""
    info = {}

    # Extract Sri Lankan phone numbers
    phone_patterns = [
        r'\b(0\d{9})\b',
        r'\b(\+94\d{9})\b',
        r'\b(94\d{9})\b'
    ]
    for pattern in phone_patterns:
        match = re.search(pattern, text)
        if match:
            info['phone'] = match.group(1)
            break

    # Extract address
    address_keywords = ['address', 'ලිපිනය', 'delivery', 'යවන්න', 'එවන්න']
    if any(keyword in text.lower() for keyword in address_keywords):
        for keyword in address_keywords:
            if keyword in text.lower():
                parts = text.lower().split(keyword)
                if len(parts) > 1:
                    info['address'] = parts[1].strip()[:200]
                    break

    # Extract name
    name_patterns = [
        r'name is ([A-Za-z\s]+)',
        r'මගේ නම ([^\n]+)',
        r'නම ([^\n]+)'
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            info['name'] = match.group(1).strip()[:50]
            break

    return info if info else None

def get_ai_response(user_message, history, products_context, language, order_detected, lead_info):
    """Generate AI response - MATCH USER'S LANGUAGE and be natural/humanized"""
    try:
        # CRITICAL: Match user's language exactly
        if language == "sinhala":
            system_prompt = """ඔබ මිත්‍රශීලී විකුණුම් සහායකයෙක්.

ප්‍රධාන නීති:
1. පරිශීලකයා සිංහලෙන් කතා කරනවා නම් සිංහලෙන්ම reply කරන්න
2. ස්වාභාවික, කෙටි පණිවිඩ (2-3 වාක්‍ය පමණ)
3. Casual Sinhala භාවිතා කරන්න: "ow", "තියනවා", "කමතිද"
4. සෑම පණිවිඩයම "Dear 💙" එකද අවසන් කරන්න

නිෂ්පාදන ගැන:
- කෙටියෙන් නම, මිල කියන්න
- වැඩි විස්තර අහනවා නම් විතරක් දෙන්න

Delivery:
- Delivery charge එක Rs.350 fixed
- Cash on Delivery available

උදාහරණ:
පරිශීලකයා: "Rack thiyanawadha?"
ඔබ: "Ow dear, 4 layer rack thiyanawa. Rs.14,500\n\nDear 💙"

Natural, friendly, casual Sinhala භාවිතා කරන්න!"""

        elif language == "singlish":
            system_prompt = """You are a friendly sales assistant.

Key rules:
1. Match user's Singlish style - mix Sinhala and English naturally
2. Keep messages short and natural (2-3 sentences)
3. Use casual tone: "ow", "thiyanawa", "kamathida"
4. End every message with "Dear 💙"

About products:
- Give name, price briefly
- More details only if asked

Delivery:
- Delivery charge is Rs.350 fixed
- Cash on Delivery available

Example:
User: "Rack thiyanawadha?"
You: "Ow dear, 4 layer rack thiyanawa. Rs.14,500\n\nDear 💙"

Be natural and friendly!"""

        else:  # English
            system_prompt = """You are a friendly sales assistant.

Key rules:
1. Keep messages short and natural (2-3 sentences)
2. Be casual and friendly
3. End every message with "Dear 💙"

About products:
- Give name, price briefly
- More details only if asked

Delivery:
- Delivery charge is Rs.350 fixed
- Cash on Delivery available

Be natural and conversational!"""

        # Add products context if available
        if products_context:
            system_prompt += f"\n\nProducts info:\n{products_context}"

        # Build messages for API
        messages = [{"role": "system", "content": system_prompt}]

        # Add conversation history (last 6 for context)
        for msg in history[-6:]:
            messages.append({"role": msg["role"], "content": msg["message"]})

        # Add current message
        messages.append({"role": "user", "content": user_message})

        # Call OpenAI - short, natural responses
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=150,  # Short and sweet
            temperature=0.8  # More natural/varied
        )

        reply = response.choices[0].message.content.strip()

        # Ensure "Dear 💙" is at the end
        if not reply.endswith("Dear 💙"):
            reply = reply + "\n\nDear 💙"

        return reply

    except Exception as e:
        print(f"OpenAI error: {e}", flush=True)
        if language == "sinhala" or language == "singlish":
            return "මට දැන් ප්‍රතිචාර දැක්වීමට අපහසුයි. කරුණාකර නැවත උත්සාහ කරන්න. Dear 💙"
        else:
            return "Sorry, I'm having trouble right now. Please try again. Dear 💙"

def get_products_for_ad(ad_id):
    """Get products from Google Sheets for specific ad_id - Returns products and image URLs"""
    try:
        sheet = get_sheet()
        if not sheet:
            return None, []

        ad_products_sheet = sheet.worksheet("Ad_Products")
        records = ad_products_sheet.get_all_records()

        for row in records:
            if str(row.get("ad_id")) == str(ad_id):
                products_text = ""
                image_urls = []

                for i in range(1, 6):  # Up to 5 products
                    name_key = f"product_{i}_name"
                    price_key = f"product_{i}_price"
                    details_key = f"product_{i}_details"
                    image_key = f"product_{i}_image_1"

                    if row.get(name_key):
                        products_text += f"{row[name_key]} - {row.get(price_key, 'N/A')}\n"

                        # Collect image URLs
                        if row.get(image_key):
                            img_url = row[image_key]
                            if img_url and img_url.startswith("http"):
                                image_urls.append(img_url)

                return products_text, image_urls

        return None, []

    except Exception as e:
        print(f"Error getting products: {e}", flush=True)
        return None, []

def send_product_images_for_ad(sender_id, ad_id, page_token):
    """Send product images at the start of conversation from ad"""
    try:
        sheet = get_sheet()
        if not sheet:
            return

        ad_products_sheet = sheet.worksheet("Ad_Products")
        records = ad_products_sheet.get_all_records()

        for row in records:
            if str(row.get("ad_id")) == str(ad_id):
                # Send images for products in this ad
                for i in range(1, 6):  # Up to 5 products
                    image_key = f"product_{i}_image_1"

                    if row.get(image_key):
                        image_url = row[image_key]
                        if image_url and image_url.startswith("http"):
                            send_image(sender_id, image_url, page_token)

                break

    except Exception as e:
        print(f"Error sending images: {e}", flush=True)

def send_image(recipient_id, image_url, page_token):
    """Send an image via Messenger"""
    if not page_token:
        print("Page token missing", flush=True)
        return

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    params = {"access_token": page_token}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {
                    "url": image_url,
                    "is_reusable": True
                }
            }
        }
    }

    r = requests.post(url, params=params, json=payload)
    print(f"Send image status: {r.status_code}, response: {r.text}", flush=True)

def search_products_by_query(query):
    """AI-powered product search for organic users - Returns products and images"""
    try:
        # Extract keywords using OpenAI
        keyword_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Extract product keywords from the user query. Return only keywords, comma separated."},
                {"role": "user", "content": query}
            ],
            max_tokens=50
        )

        keywords = keyword_response.choices[0].message.content.lower()

        # Search in Google Sheets
        sheet = get_sheet()
        if not sheet:
            return None, []

        ad_products_sheet = sheet.worksheet("Ad_Products")
        records = ad_products_sheet.get_all_records()

        found_products = []
        found_images = []

        for row in records:
            for i in range(1, 6):
                name = str(row.get(f"product_{i}_name", "")).lower()
                details = str(row.get(f"product_{i}_details", "")).lower()

                if any(kw.strip() in name or kw.strip() in details for kw in keywords.split(",")):
                    product_info = {
                        "name": row.get(f"product_{i}_name"),
                        "price": row.get(f"product_{i}_price"),
                    }
                    if product_info not in found_products:
                        found_products.append(product_info)

                        # Get image
                        img_url = row.get(f"product_{i}_image_1")
                        if img_url and img_url.startswith("http"):
                            found_images.append(img_url)

        if found_products:
            products_text = ""
            for prod in found_products[:5]:
                products_text += f"{prod['name']} - {prod['price']}\n"

            return products_text, found_images[:5]

        return None, []

    except Exception as e:
        print(f"Error in product search: {e}", flush=True)
        return None, []

def save_message(sender_id, ad_id, role, message):
    """Save message to Conversations sheet"""
    try:
        sheet = get_sheet()
        if not sheet:
            return

        conversations_sheet = sheet.worksheet("Conversations")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conversations_sheet.append_row([
            sender_id,
            ad_id or "",
            timestamp,
            role,
            message
        ])

    except Exception as e:
        print(f"Error saving message: {e}", flush=True)

def get_conversation_history(sender_id):
    """Get last messages for this user"""
    try:
        sheet = get_sheet()
        if not sheet:
            return []

        conversations_sheet = sheet.worksheet("Conversations")
        records = conversations_sheet.get_all_records()

        user_messages = [r for r in records if str(r.get("sender_id")) == str(sender_id)]
        user_messages = user_messages[-8:]  # Last 8 messages

        return [{"role": m["role"], "message": m["message"]} for m in user_messages if m["role"] in ["user", "assistant"]]

    except Exception as e:
        print(f"Error getting history: {e}", flush=True)
        return []

def get_user_ad_id(sender_id):
    """Get ad_id for this user from Conversations"""
    try:
        sheet = get_sheet()
        if not sheet:
            return None

        conversations_sheet = sheet.worksheet("Conversations")
        records = conversations_sheet.get_all_records()

        for record in reversed(records):
            if str(record.get("sender_id")) == str(sender_id):
                ad_id = record.get("ad_id")
                if ad_id:
                    return ad_id

        return None

    except Exception as e:
        print(f"Error getting ad_id: {e}", flush=True)
        return None

def save_lead(sender_id, ad_id, lead_info):
    """Save/update lead information"""
    try:
        sheet = get_sheet()
        if not sheet:
            return

        leads_sheet = sheet.worksheet("Leads")
        records = leads_sheet.get_all_records()

        # Check if lead exists
        row_index = None
        for idx, record in enumerate(records, start=2):
            if str(record.get("Sender ID")) == str(sender_id):
                row_index = idx
                break

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if row_index:
            # Update existing lead
            if lead_info.get('name'):
                leads_sheet.update_cell(row_index, 3, lead_info['name'])
            if lead_info.get('address'):
                leads_sheet.update_cell(row_index, 4, lead_info['address'])
            if lead_info.get('phone'):
                leads_sheet.update_cell(row_index, 5, lead_info['phone'])
        else:
            # New lead
            leads_sheet.append_row([
                sender_id,
                ad_id or "",
                lead_info.get('name', ''),
                lead_info.get('address', ''),
                lead_info.get('phone', ''),
                "",  # Product Name
                timestamp,
                "new"
            ])

    except Exception as e:
        print(f"Error saving lead: {e}", flush=True)

def save_order_to_leads(sender_id, ad_id, lead_info, products_context):
    """Save order details to Leads tab after order is placed"""
    try:
        sheet = get_sheet()
        if not sheet:
            return

        leads_sheet = sheet.worksheet("Leads")
        records = leads_sheet.get_all_records()

        # Find the lead
        row_index = None
        for idx, record in enumerate(records, start=2):
            if str(record.get("Sender ID")) == str(sender_id):
                row_index = idx
                break

        # Extract product name
        product_name = "Order Placed"
        if products_context:
            lines = products_context.split('\n')
            for line in lines:
                if line.strip():
                    product_name = line.strip().split(' - ')[0][:50]
                    break

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if row_index:
            leads_sheet.update_cell(row_index, 6, product_name)  # Product Name
            leads_sheet.update_cell(row_index, 7, timestamp)  # Date
            leads_sheet.update_cell(row_index, 8, "ordered")  # Status

    except Exception as e:
        print(f"Error saving order: {e}", flush=True)

def send_message(recipient_id, text, page_token):
    """Send text message via Messenger"""
    if not page_token:
        print("Page token missing", flush=True)
        return

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    params = {"access_token": page_token}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }

    r = requests.post(url, params=params, json=payload)
    print(f"Send message status: {r.status_code}, response: {r.text}", flush=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
