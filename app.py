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

# User conversation state tracking
user_states = {}

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
        products_context, product_images = get_products_for_ad(ad_id)

        # Send product details first
        if products_context:
            product_message = f"Mehenna ape products:\n\n{products_context}"
            send_message(sender_id, product_message, page_token)
            save_message(sender_id, ad_id, "assistant", product_message)

        # Send ALL product images (up to 3 images per product)
        if product_images:
            for img_url in product_images[:10]:  # Send up to 10 images
                send_image(sender_id, img_url, page_token)

        # Initialize conversation flow - Start with location question
        user_states[sender_id] = {"step": "ask_location", "ad_id": ad_id, "product": products_context}

        # Ask for location
        location_msg = "Location eka kohada?\n\nDear 💙"
        send_message(sender_id, location_msg, page_token)
        save_message(sender_id, ad_id, "assistant", location_msg)

        print(f"Ad referral: sender={sender_id}, ad_id={ad_id}", flush=True)
    except Exception as e:
        print(f"Error in handle_ad_referral: {e}", flush=True)

def handle_message(sender_id, text, page_token):
    """Main message handler with FLEXIBLE flow"""
    try:
        # Get user's ad_id
        ad_id = get_user_ad_id(sender_id)

        # Save user message
        save_message(sender_id, ad_id, "user", text)

        # Detect language
        language = detect_language(text)

        # CHECK IF USER WANTS TO PLACE ORDER DIRECTLY (skip flow)
        if detect_direct_order_intent(text):
            handle_direct_order(sender_id, text, page_token, ad_id)
            return

        # Check if user is asking about delivery
        is_delivery_question = detect_delivery_question(text)

        # If asking about delivery, need location first
        if is_delivery_question and sender_id not in user_states:
            # Start flow from location
            products_context, _ = get_products_for_ad(ad_id) if ad_id else (None, [])
            user_states[sender_id] = {"step": "ask_location_for_delivery", "ad_id": ad_id, "product": products_context}

            location_msg = "Location eka kohada? Ekata delivery charge eka kiyanna puluwanda.\n\nDear 💙"
            send_message(sender_id, location_msg, page_token)
            save_message(sender_id, ad_id, "assistant", location_msg)
            return

        # Check if user is asking a question (might want to pause flow)
        is_question = detect_question(text)

        # Check if user is in a conversation flow
        if sender_id in user_states and not is_question:
            handle_conversation_flow(sender_id, text, page_token, language)
        else:
            # If user asks question during flow, answer it but keep flow active
            if sender_id in user_states and is_question:
                # Answer question without breaking flow
                answer_question_in_flow(sender_id, text, page_token, language, ad_id)
            else:
                # Regular conversation (organic user or after flow completed)
                handle_regular_conversation(sender_id, text, page_token, language, ad_id)

    except Exception as e:
        print(f"Error in handle_message: {e}", flush=True)
        send_message(sender_id, "Sorry, I'm having trouble. Please try again. Dear 💙", page_token)

def detect_direct_order_intent(text):
    """Detect if user directly wants to place order"""
    direct_order_keywords = [
        'order karanna', 'order karanawa', 'ganna', 'gannawa', 
        'place order', 'i want to order', 'මං order කරන්න', 
        'එක ගන්නම්', 'order karanna oni', 'දෙන්න', 'eka ganna'
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in direct_order_keywords)

def detect_delivery_question(text):
    """Detect if user is asking about delivery"""
    delivery_keywords = ['delivery', 'delivery kiyada', 'delivery charge', 'යවන්නේ', 
                        'කොහොමද යවන්නේ', 'dawas', 'dawas kiyak', 'kiyak yanawada']
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in delivery_keywords)

def detect_question(text):
    """Detect if user is asking a question"""
    question_indicators = ['?', 'thiyanawada', 'thiyenawada', 'mokakda', 'kohomada', 
                          'kiyada', 'what', 'how', 'when', 'කොහොමද', 'මොකක්ද', 
                          'තියෙනවද', 'කීයද', 'වර්ණ', 'color', 'size', 'wena',
                          'monawada', 'mokada']
    text_lower = text.lower()
    # Don't treat delivery questions as regular questions during details collection
    if detect_delivery_question(text):
        return False
    return '?' in text or any(indicator in text_lower for indicator in question_indicators)

def handle_direct_order(sender_id, text, page_token, ad_id):
    """Handle when user directly says they want to order"""
    # Skip flow, go straight to collecting details
    products_context, _ = get_products_for_ad(ad_id) if ad_id else (None, [])

    user_states[sender_id] = {
        "step": "collect_details_direct", 
        "ad_id": ad_id, 
        "product": products_context
    }

    # Ask for all details
    details_msg = "Super! Meh details ewanna puluwanda:\n\n1. Name\n2. Address (full address)\n3. Phone number\n4. Quantity (keeyek onda?)\n\nDear 💙"
    send_message(sender_id, details_msg, page_token)
    save_message(sender_id, ad_id, "assistant", details_msg)

def answer_question_in_flow(sender_id, text, page_token, language, ad_id):
    """Answer user's question while maintaining flow state"""
    state = user_states[sender_id]
    products_context = state.get("product")

    # Get conversation history
    history = get_conversation_history(sender_id, limit=8)

    # Generate answer using AI
    reply = get_ai_response(text, history, products_context, language)

    # Send answer
    send_message(sender_id, reply, page_token)
    save_message(sender_id, ad_id, "assistant", reply)

    # ONLY continue flow if not in details collection step
    step = state.get("step")
    if step not in ["collect_details", "collect_details_direct"]:
        if step == "ask_location":
            follow_up = "Location eka kohada? 😊\n\nDear 💙"
        elif step == "confirm_delivery":
            follow_up = "Delivery charge Rs.350 ok neda?\n\nDear 💙"
        elif step == "ask_order":
            follow_up = "Order karanna kamathi dha?\n\nDear 💙"
        else:
            return

        send_message(sender_id, follow_up, page_token)
        save_message(sender_id, ad_id, "assistant", follow_up)

def handle_conversation_flow(sender_id, text, page_token, language):
    """Handle step-by-step conversation flow"""
    state = user_states[sender_id]
    step = state.get("step")
    ad_id = state.get("ad_id")

    if step == "ask_location" or step == "ask_location_for_delivery":
        # User provided location
        state["location"] = text
        state["step"] = "confirm_delivery"

        # Tell delivery charge
        delivery_msg = "Hari! Delivery charge eka Rs.350 yi. Eka ok neda?\n\nDear 💙"
        send_message(sender_id, delivery_msg, page_token)
        save_message(sender_id, ad_id, "assistant", delivery_msg)

    elif step == "confirm_delivery":
        # Check if user agrees
        agrees = check_agreement(text)

        if agrees:
            state["step"] = "ask_order"

            # Ask if they want to order
            order_msg = "Order karanna kamathi dha?\n\nDear 💙"
            send_message(sender_id, order_msg, page_token)
            save_message(sender_id, ad_id, "assistant", order_msg)
        else:
            # Not interested, end flow
            goodbye_msg = "Hari dear, awashya welawaka mata call karanna!\n\nDear 💙"
            send_message(sender_id, goodbye_msg, page_token)
            save_message(sender_id, ad_id, "assistant", goodbye_msg)
            del user_states[sender_id]

    elif step == "ask_order":
        # Check if user wants to order
        wants_order = check_agreement(text)

        if wants_order:
            state["step"] = "collect_details"

            # Ask for all details
            details_msg = "Super! Meh details ewanna puluwanda:\n\n1. Name\n2. Address (full address)\n3. Phone number\n4. Quantity (keeyek onda?)\n\nDear 💙"
            send_message(sender_id, details_msg, page_token)
            save_message(sender_id, ad_id, "assistant", details_msg)
        else:
            # Not ordering, end flow
            goodbye_msg = "Hari dear, prashna thiyenawannam ahanna!\n\nDear 💙"
            send_message(sender_id, goodbye_msg, page_token)
            save_message(sender_id, ad_id, "assistant", goodbye_msg)
            del user_states[sender_id]

    elif step == "collect_details" or step == "collect_details_direct":
        # Extract all details from message
        lead_info = extract_full_lead_info(text)

        # Need at least name AND phone
        if lead_info.get("phone") and lead_info.get("name"):
            # Save to Leads with product
            save_complete_order(sender_id, ad_id, lead_info, state.get("product"))

            # Confirm order
            confirm_msg = f"Order eka confirm! {lead_info.get('phone')} ekata call karala delivery arrange karanawa. Thank you!\n\nDear 💙"
            send_message(sender_id, confirm_msg, page_token)
            save_message(sender_id, ad_id, "assistant", confirm_msg)

            # End flow
            del user_states[sender_id]
        else:
            # Missing details, ask again
            missing = []
            if not lead_info.get("name"):
                missing.append("Name")
            if not lead_info.get("address"):
                missing.append("Address")
            if not lead_info.get("phone"):
                missing.append("Phone number")
            if not lead_info.get("quantity"):
                missing.append("Quantity")

            retry_msg = f"{', '.join(missing)} missing. Karuna karala full details ewanna:\n\nName\nAddress\nPhone\nQuantity\n\nDear 💙"
            send_message(sender_id, retry_msg, page_token)
            save_message(sender_id, ad_id, "assistant", retry_msg)

def handle_regular_conversation(sender_id, text, page_token, language, ad_id):
    """Handle regular conversations (non-flow)"""
    # Check if user wants photos
    wants_photos = detect_photo_request(text)

    # Get conversation history
    history = get_conversation_history(sender_id, limit=10)

    # Get products
    products_context = None
    product_images = []

    if ad_id:
        products_context, product_images = get_products_for_ad(ad_id)
    else:
        # For organic users, search products
        products_context, product_images = search_products_by_query(text)

        # If found products, send details and images
        if products_context:
            product_msg = f"Mehenna ape products:\n\n{products_context}"
            send_message(sender_id, product_msg, page_token)
            save_message(sender_id, ad_id, "assistant", product_msg)

            # Send ALL images
            if product_images:
                for img_url in product_images[:10]:
                    send_image(sender_id, img_url, page_token)

            # Start conversation flow
            user_states[sender_id] = {"step": "ask_location", "ad_id": ad_id, "product": products_context}

            location_msg = "Location eka kohada?\n\nDear 💙"
            send_message(sender_id, location_msg, page_token)
            save_message(sender_id, ad_id, "assistant", location_msg)
            return

    # If user wants photos, send them
    if wants_photos and product_images:
        for img_url in product_images[:10]:
            send_image(sender_id, img_url, page_token)
        reply_text = "Mehenna photos! Order karanna kamathi dha?\n\nDear 💙"
    else:
        # Generate AI response
        reply_text = get_ai_response(text, history, products_context, language)

    # Save and send
    save_message(sender_id, ad_id, "assistant", reply_text)
    send_message(sender_id, reply_text, page_token)

def check_agreement(text):
    """Check if user agrees/says yes"""
    agreement_keywords = ['yes', 'ow', 'හරි', 'ඔව්', 'ok', 'oka', 'එහෙනම්', 'ඕනා', 'කැමති', 'kamathi', 'hari']
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in agreement_keywords)

def extract_full_lead_info(text):
    """Extract name, address, phone, quantity from detailed message"""
    info = {}

    # Extract phone
    phone_patterns = [
        r'(0\d{9})',
        r'(\+94\d{9})',
        r'(94\d{9})'
    ]
    for pattern in phone_patterns:
        match = re.search(pattern, text.replace(' ', '').replace('-', ''))
        if match:
            info['phone'] = match.group(1)
            break

    # Extract quantity - improve to capture "1" alone
    qty_patterns = [
        r'(?:qty|quantity|කීයක්|ප්‍රමාණය|keeyek)[:\s]*(\d+)',
        r'(\d+)\s*(?:ක්|එකක්|න්|ekak|ganna|layer|tier)',
        r'\n(\d+)\s*$',  # Number at end of line
        r'^(\d+)$',  # Just a number
        r'\s(\d+)\s*(?:\n|$)'  # Number with newline
    ]
    for pattern in qty_patterns:
        match = re.search(pattern, text.lower())
        if match:
            info['quantity'] = match.group(1)
            break

    # If no quantity found but there's a single digit number, use it
    if not info.get('quantity'):
        single_num = re.search(r'(?:^|\s)(\d+)(?:\s|$)', text)
        if single_num:
            info['quantity'] = single_num.group(1)

    # Extract name - improved to get first line or capitalized words
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    for i, line in enumerate(lines):
        if any(word in line.lower() for word in ['name', 'නම', 'nama']):
            name_match = re.search(r'(?:name|නම|nama)[:\s]*([A-Z][a-zA-Z\s]{1,40})', line, re.IGNORECASE)
            if name_match:
                info['name'] = name_match.group(1).strip()
                break
        # First line with capitalized word (likely name)
        elif i == 0 and re.match(r'^[A-Z][a-z]+', line):
            info['name'] = line[:50]
            break
        # Line with just capitalized words
        elif re.match(r'^[A-Z][a-z]+(\s+[A-Z][a-z]+)*$', line):
            info['name'] = line[:50]
            break

    # Extract address - second/third line or lines with location indicators
    address_lines = []
    for i, line in enumerate(lines):
        if any(indicator in line.lower() for indicator in ['no:', 'no.', 'road', 'street', 'galle', 
                                                           'colombo', 'kandy', 'kurunegala', 'negombo',
                                                           'address', 'ලිපිනය']):
            address_lines.append(line)
        # Second line is often address (if not phone or qty)
        elif i == 1 and not re.search(r'\d{9,10}', line.replace(' ', '')) and len(line) > 5:
            if not re.match(r'^\d+$', line):
                address_lines.append(line)

    if address_lines:
        info['address'] = ' '.join(address_lines)[:200]

    return info

def save_complete_order(sender_id, ad_id, lead_info, products_context):
    """Save complete order to Leads sheet"""
    try:
        sheet = get_sheet()
        if not sheet:
            return

        leads_sheet = sheet.worksheet("Leads")

        # Extract product name - improved to capture "4 layer rack" etc
        product_name = "Order Placed"
        if products_context:
            lines = products_context.split('\n')
            for line in lines:
                if line.strip() and ' - ' in line:
                    product_name = line.strip().split(' - ')[0][:50]
                    break

        # Add quantity to product name
        if lead_info.get('quantity'):
            product_name = f"{product_name} (Qty: {lead_info['quantity']})"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Save new order
        leads_sheet.append_row([
            sender_id,
            ad_id or "",
            lead_info.get('name', ''),
            lead_info.get('address', ''),
            lead_info.get('phone', ''),
            product_name,
            timestamp,
            "ordered"
        ])

        print(f"Saved complete order: {lead_info}", flush=True)

    except Exception as e:
        print(f"Error saving order: {e}", flush=True)

def detect_language(text):
    """Detect if user is speaking Sinhala, English, or Singlish"""
    sinhala_pattern = re.compile('[\u0D80-\u0DFF]')
    has_sinhala = bool(sinhala_pattern.search(text))

    english_words = re.findall(r'\b[a-zA-Z]+\b', text)
    has_english = len(english_words) > 0

    if has_sinhala and has_english:
        return "singlish"
    elif has_sinhala:
        return "sinhala"
    else:
        return "english"

def detect_photo_request(text):
    """Detect if user wants to see photos"""
    photo_keywords = ['photo', 'photos', 'pic', 'pics', 'picture', 'image', 
                      'wena', 'පින්තූර', 'photo එක', 'pics දෙන්න', 'photo danna']
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in photo_keywords)

def get_ai_response(user_message, history, products_context, language):
    """Generate AI response - SHORT, NATURAL casual Singlish"""
    try:
        # CASUAL SINGLISH system prompt
        system_prompt = """You are a friendly sales assistant. Respond in CASUAL SINGLISH.

Rules:
1. Very short (1-2 sentences max)
2. Natural Singlish: "ow", "thiyanawa", "kiyada", "mehenna", "kamathi dha", "harida"
3. Remember conversation context
4. Only mention products that exist
5. End with "Dear 💙"
6. "Mona" means "what" in Sinhala - ask which product they want
7. Layer/tier racks - always say "4 layer rack", "3 layer rack" etc

Examples:
- "Ow dear, thiyanawa. Rs.5000 yi."
- "Mehenna rack eka. Order karanna kamathi dha?"
- "Blue, Red colors thiyanawa dear."
- "Location eka kohada?"
- "Delivery 3-5 days yanawa dear."

Delivery: Rs.350 fixed, 3-5 working days, COD available"""

        if products_context:
            system_prompt += f"\n\nProducts:\n{products_context}"

        messages = [{"role": "system", "content": system_prompt}]

        for msg in history[-8:]:
            messages.append({"role": msg["role"], "content": msg["message"]})

        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=80,
            temperature=0.7
        )

        reply = response.choices[0].message.content.strip()

        if not reply.endswith("Dear 💙"):
            reply = reply + "\n\nDear 💙"

        return reply

    except Exception as e:
        print(f"OpenAI error: {e}", flush=True)
        return "Sorry, having trouble. Dear 💙"

def get_products_for_ad(ad_id):
    """Get products from Google Sheets for specific ad_id - ALL IMAGES"""
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

                # Get all products (up to 5)
                for i in range(1, 6):
                    name_key = f"product_{i}_name"
                    price_key = f"product_{i}_price"

                    if row.get(name_key):
                        products_text += f"{row[name_key]} - {row.get(price_key, '')}\n"

                        # Get ALL 3 images for each product
                        for img_num in range(1, 4):
                            image_key = f"product_{i}_image_{img_num}"
                            if row.get(image_key):
                                img_url = row[image_key]
                                if img_url and img_url.startswith("http"):
                                    image_urls.append(img_url)

                return products_text, image_urls

        return None, []

    except Exception as e:
        print(f"Error getting products: {e}", flush=True)
        return None, []

def send_image(recipient_id, image_url, page_token):
    """Send an image via Messenger"""
    if not page_token:
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
    print(f"Send image: {r.status_code}", flush=True)

def search_products_by_query(query):
    """AI-powered product search"""
    try:
        keyword_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Extract product keywords. Return only keywords, comma separated."},
                {"role": "user", "content": query}
            ],
            max_tokens=30
        )

        keywords = keyword_response.choices[0].message.content.lower()

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

                if name and any(kw.strip() in name for kw in keywords.split(",")):
                    prod_name = row.get(f"product_{i}_name")
                    prod_price = row.get(f"product_{i}_price")

                    if prod_name and prod_name not in [p['name'] for p in found_products]:
                        found_products.append({"name": prod_name, "price": prod_price})

                        # Get all 3 images
                        for img_num in range(1, 4):
                            img_url = row.get(f"product_{i}_image_{img_num}")
                            if img_url and img_url.startswith("http"):
                                found_images.append(img_url)

        if found_products:
            products_text = ""
            for prod in found_products[:3]:
                products_text += f"{prod['name']} - {prod['price']}\n"

            return products_text, found_images[:10]

        return None, []

    except Exception as e:
        print(f"Error in search: {e}", flush=True)
        return None, []

def save_message(sender_id, ad_id, role, message):
    """Save message to Conversations"""
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

def get_conversation_history(sender_id, limit=10):
    """Get conversation history"""
    try:
        sheet = get_sheet()
        if not sheet:
            return []

        conversations_sheet = sheet.worksheet("Conversations")
        records = conversations_sheet.get_all_records()

        user_messages = [r for r in records if str(r.get("sender_id")) == str(sender_id)]
        user_messages = user_messages[-limit:]

        return [{"role": m["role"], "message": m["message"]} for m in user_messages if m["role"] in ["user", "assistant"]]

    except Exception as e:
        print(f"Error getting history: {e}", flush=True)
        return []

def get_user_ad_id(sender_id):
    """Get ad_id for this user"""
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

def send_message(recipient_id, text, page_token):
    """Send text message"""
    if not page_token:
        return

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    params = {"access_token": page_token}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }

    r = requests.post(url, params=params, json=payload)
    print(f"Send message: {r.status_code}", flush=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
