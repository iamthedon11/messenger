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

        # Get products for this ad
        products = get_products_for_ad(ad_id)

        if products:
            # Send product images at start of conversation
            send_product_images(sender_id, products, page_token)

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

        # Check for order placement (before lead extraction)
        order_detected = detect_order_placement(text)

        # Extract lead info (phone, address, name)
        lead_info = extract_lead_info(text)
        if lead_info:
            save_lead(sender_id, ad_id, lead_info)

        # Detect language preference
        language = detect_language(text)

        # Get conversation history
        history = get_conversation_history(sender_id)

        # Get products
        if ad_id:
            products_context = get_products_for_ad(ad_id)
        else:
            products_context = search_products_by_query(text)

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
        r'\b(0\d{9})\b',  # 0771234567
        r'\b(\+94\d{9})\b',  # +94771234567
        r'\b(94\d{9})\b'  # 94771234567
    ]
    for pattern in phone_patterns:
        match = re.search(pattern, text)
        if match:
            info['phone'] = match.group(1)
            break

    # Extract address (look for common address keywords)
    address_keywords = ['address', 'ලිපිනය', 'delivery', 'යවන්න', 'එවන්න']
    if any(keyword in text.lower() for keyword in address_keywords):
        # Simple extraction: take the text after address keyword
        for keyword in address_keywords:
            if keyword in text.lower():
                parts = text.lower().split(keyword)
                if len(parts) > 1:
                    info['address'] = parts[1].strip()[:200]  # Limit length
                    break

    # Extract name (look for "name is" or "නම" patterns)
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
    """Generate AI response with lengthy, detailed messages in Sinhala"""
    try:
        # Build system prompt - UPDATED FOR LENGTHY RESPONSES IN SINHALA
        if language == "sinhala" or language == "singlish":
            system_prompt = """ඔබ විශිෂ්ට විකුණුම් සහායකයෙක්. ඔබේ භූමිකාව:

1. සෑම පණිවිඩයකම සවිස්තරාත්මක හා දිගු පිළිතුරු දෙන්න
2. නිෂ්පාදන ගැන සම්පූර්ණ විස්තර දෙන්න - මිල, විශේෂාංග, ප්‍රතිලාභ
3. වචන 100-200 අතර දිගු පිළිතුරු ලියන්න
4. මිත්‍රශීලී, උණුසුම්, සවිස්තරාත්මක පණිවිඩ යවන්න
5. සෑම පණිවිඩයක්ම "Dear 💙" සමග අවසන් කරන්න

ව්‍යාවහාරික රටාව:
- නිෂ්පාදන ගැන විස්තර කරන විට සියලු විශේෂාංග සඳහන් කරන්න
- ගනුදෙනුකරුට ඇයි මේක හොඳද කියා පැහැදිලි කරන්න
- මිල ගැන කතා කරන විට වටිනාකම පැහැදිලි කරන්න
- "ඕඩර් කරන්න කැමතිද?" වගේ අවසන් ප්‍රශ්න අහන්න
- Cash on Delivery තියෙනවා කියන්න

නිෂ්පාදන තිබේ නම්: ලබා දී ඇති නිශ්චිත තොරතුරු භාවිතා කරන්න (සිංහලෙන්)
කිසිවිටෙක: තොරතුරු සාදන්න එපා, දිනයන් පොරොන්දු වෙන්න එපා"""
        else:
            system_prompt = """You are an excellent sales assistant. Your role:

1. Provide detailed and lengthy responses in every message
2. Give complete product information - price, features, benefits
3. Write responses between 100-200 words
4. Be friendly, warm, and detailed
5. End every message with "Dear 💙"

Conversational pattern:
- When describing products, mention all features
- Explain why this product is good for the customer
- When discussing price, clarify the value
- Ask closing questions like "Would you like to order?"
- Mention Cash on Delivery is available

If products available: Use exact details provided
Never: Make up information, promise dates"""

        # Add products context
        if products_context:
            system_prompt += f"\n\nනිෂ්පාදන තොරතුරු:\n{products_context}"

        # Build messages for API
        messages = [{"role": "system", "content": system_prompt}]

        # Add conversation history
        for msg in history[-10:]:
            messages.append({"role": msg["role"], "content": msg["message"]})

        # Add current message
        messages.append({"role": "user", "content": user_message})

        # Call OpenAI with increased max_tokens for lengthy responses
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=500,  # INCREASED from 150 to 500 for lengthy messages
            temperature=0.7
        )

        reply = response.choices[0].message.content

        # Ensure "Dear 💙" is at the end
        if not reply.strip().endswith("Dear 💙"):
            reply = reply.strip() + "\n\nDear 💙"

        return reply

    except Exception as e:
        print(f"OpenAI error: {e}", flush=True)
        return "මට දැන් ප්‍රතිචාර දැක්වීමට අපහසුයි. කරුණාකර නැවත උත්සාහ කරන්න. Dear 💙"

def get_products_for_ad(ad_id):
    """Get products from Google Sheets for specific ad_id - RETURNS SINHALA TEXT"""
    try:
        sheet = get_sheet()
        if not sheet:
            return None

        ad_products_sheet = sheet.worksheet("Ad_Products")
        records = ad_products_sheet.get_all_records()

        for row in records:
            if str(row.get("ad_id")) == str(ad_id):
                # Build Sinhala product description
                products_text = f"මෙම දැන්වීමේ නිෂ්පාදන:\n\n"

                for i in range(1, 6):  # Up to 5 products
                    name_key = f"product_{i}_name"
                    price_key = f"product_{i}_price"
                    details_key = f"product_{i}_details"

                    if row.get(name_key):
                        products_text += f"{i}. {row[name_key]}\n"
                        products_text += f"   මිල: {row.get(price_key, 'N/A')}\n"
                        products_text += f"   විස්තර: {row.get(details_key, 'N/A')}\n\n"

                return products_text

        return None

    except Exception as e:
        print(f"Error getting products: {e}", flush=True)
        return None

def send_product_images(sender_id, products_data, page_token):
    """Send product images at the start of conversation"""
    try:
        sheet = get_sheet()
        if not sheet:
            return

        ad_products_sheet = sheet.worksheet("Ad_Products")
        records = ad_products_sheet.get_all_records()

        # Find the ad_id for this sender
        ad_id = get_user_ad_id(sender_id)
        if not ad_id:
            return

        # Find product images
        for row in records:
            if str(row.get("ad_id")) == str(ad_id):
                # Send images for each product
                for i in range(1, 6):  # Up to 5 products
                    image_key = f"product_{i}_image_1"
                    product_name = row.get(f"product_{i}_name")

                    if row.get(image_key) and product_name:
                        image_url = row[image_key]
                        if image_url and image_url.startswith("http"):
                            send_image(sender_id, image_url, page_token)

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
    """AI-powered product search for organic users"""
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
            return None

        ad_products_sheet = sheet.worksheet("Ad_Products")
        records = ad_products_sheet.get_all_records()

        found_products = []
        for row in records:
            for i in range(1, 6):
                name = str(row.get(f"product_{i}_name", "")).lower()
                details = str(row.get(f"product_{i}_details", "")).lower()

                if any(kw in name or kw in details for kw in keywords.split(",")):
                    found_products.append({
                        "name": row.get(f"product_{i}_name"),
                        "price": row.get(f"product_{i}_price"),
                        "details": row.get(f"product_{i}_details")
                    })

        if found_products:
            products_text = "Available products:\n\n"
            for idx, prod in enumerate(found_products[:5], 1):
                products_text += f"{idx}. {prod['name']}\n"
                products_text += f"   Price: {prod['price']}\n"
                products_text += f"   Details: {prod['details']}\n\n"
            return products_text

        return None

    except Exception as e:
        print(f"Error in product search: {e}", flush=True)
        return None

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
    """Get last 10 messages for this user"""
    try:
        sheet = get_sheet()
        if not sheet:
            return []

        conversations_sheet = sheet.worksheet("Conversations")
        records = conversations_sheet.get_all_records()

        user_messages = [r for r in records if str(r.get("sender_id")) == str(sender_id)]
        user_messages = user_messages[-10:]  # Last 10

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
        for idx, record in enumerate(records, start=2):  # Start at 2 (header is row 1)
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
                "",  # Product Name (will be filled on order)
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

        # Extract product name from products_context
        product_name = "Order Placed"
        if products_context:
            lines = products_context.split('\n')
            for line in lines:
                if line.strip() and not line.startswith('මෙම'):
                    product_name = line.strip()[:50]
                    break

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if row_index:
            # Update existing lead with order details
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
