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


# =====================
# Google Sheets helpers
# =====================

def get_sheet():
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDS)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        return gc.open(SHEET_NAME)
    except Exception as e:
        print(f"Google Sheets connection error: {e}", flush=True)
        return None


# =========
# Endpoints
# =========

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


# ===================
# Core flow handlers
# ===================

def handle_ad_referral(sender_id, ad_id, page_token):
    """Handle new user from Click-to-Messenger ad"""
    try:
        save_message(sender_id, ad_id, "system", f"User arrived from ad {ad_id}")

        products_context, product_images = get_products_for_ad(ad_id)

        # Send product list
        if products_context:
            product_message = f"Mehenna ape products:\n\n{products_context}"
            send_message(sender_id, product_message, page_token)
            save_message(sender_id, ad_id, "assistant", product_message)

        # Send images (up to 10)
        if product_images:
            for img_url in product_images[:10]:
                send_image(sender_id, img_url, page_token)

        # Start flow
        user_states[sender_id] = {"step": "ask_location", "ad_id": ad_id, "product": products_context}

        location_msg = "Location eka kohada?\n\nDear 💙"
        send_message(sender_id, location_msg, page_token)
        save_message(sender_id, ad_id, "assistant", location_msg)

        print(f"Ad referral: sender={sender_id}, ad_id={ad_id}", flush=True)
    except Exception as e:
        print(f"Error in handle_ad_referral: {e}", flush=True)


def handle_message(sender_id, text, page_token):
    """Main message handler - AI-first with safety rails"""
    try:
        ad_id = get_user_ad_id(sender_id)
        save_message(sender_id, ad_id, "user", text)

        # Get products and history for context
        products_context, product_images = get_products_for_ad(ad_id) if ad_id else (None, [])
        history = get_conversation_history(sender_id, limit=10)
        
        # CRITICAL: Check if user is sending complete contact details
        if detect_contact_details(text):
            handle_contact_details(sender_id, text, page_token, ad_id, products_context)
            return

        # If in flow, handle flow logic
        if sender_id in user_states:
            step = user_states[sender_id].get("step")
            
            # Location step
            if step in ["ask_location", "ask_location_for_delivery"]:
                user_states[sender_id]["location"] = text
                user_states[sender_id]["step"] = "ask_order"
                
                combined_msg = "Hari! Delivery charge eka Rs.350 yi. Order karanna kamathi dha?\n\nDear 💙"
                send_message(sender_id, combined_msg, page_token)
                save_message(sender_id, ad_id, "assistant", combined_msg)
                return
            
            # Order confirmation step
            elif step == "ask_order":
                wants_order = check_agreement(text)
                
                if wants_order:
                    user_states[sender_id]["step"] = "collect_details"
                    details_msg = "Super! Meh details ewanna puluwanda:\n\n1. Name\n2. Address\n3. Phone number\n\nDear 💙"
                    send_message(sender_id, details_msg, page_token)
                    save_message(sender_id, ad_id, "assistant", details_msg)
                    return
                else:
                    goodbye_msg = "Hari dear, prashna thiyenawannam ahanna!\n\nDear 💙"
                    send_message(sender_id, goodbye_msg, page_token)
                    save_message(sender_id, ad_id, "assistant", goodbye_msg)
                    del user_states[sender_id]
                    return
            
            # Collecting details - let AI handle it naturally
            elif step in ["collect_details", "collect_details_direct"]:
                # Check if they provided details
                lead_info = extract_full_lead_info(text)
                if lead_info.get("phone"):
                    handle_contact_details(sender_id, text, page_token, ad_id, products_context)
                    return
                else:
                    # AI will prompt for missing details
                    pass

        # Use AI for natural conversation
        reply = get_ai_response(text, history, products_context, product_images, sender_id, ad_id)
        
        # Check if AI wants to send images
        if "SEND_IMAGES" in reply:
            reply = reply.replace("SEND_IMAGES", "").strip()
            if product_images:
                for img_url in product_images[:10]:
                    send_image(sender_id, img_url, page_token)
        
        # Check if AI wants to start flow
        if "START_LOCATION_FLOW" in reply:
            reply = reply.replace("START_LOCATION_FLOW", "").strip()
            user_states[sender_id] = {
                "step": "ask_location",
                "ad_id": ad_id,
                "product": products_context
            }
        
        send_message(sender_id, reply, page_token)
        save_message(sender_id, ad_id, "assistant", reply)

    except Exception as e:
        print(f"Error in handle_message: {e}", flush=True)
        send_message(sender_id, "Sorry dear, technical issue ekak. Try again karanna!\n\nDear 💙", page_token)


# -------------
# Intent helpers
# -------------

def detect_contact_details(text):
    """Detect if message contains phone number + other details"""
    has_phone = bool(re.search(r'0\d{9}|94\d{9}|\+94\d{9}', text.replace(' ', '').replace('-', '')))
    
    # Check for address or name indicators
    address_indicators = ['no:', 'no.', 'road', 'street', 'colombo', 'kandy', 'galle', 'negombo', 'kurunegala', 'matara', 'anuradhapura']
    has_address = any(indicator in text.lower() for indicator in address_indicators)
    
    has_name = bool(re.search(r'[A-Z][a-z]+\s+[A-Z][a-z]+', text))
    
    # Multi-line with phone suggests full details
    has_multiple_lines = len([l for l in text.split('\n') if l.strip()]) >= 2
    
    return has_phone and (has_address or has_name or has_multiple_lines)


def check_agreement(text):
    """Check if user agrees/says yes"""
    agreement_keywords = [
        "yes", "ow", "හරි", "ඔව්", "ok", "oka", "එහෙනම්",
        "ඕනා", "කැමති", "kamathi", "hari", "okey"
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in agreement_keywords)


def handle_contact_details(sender_id, text, page_token, ad_id, products_context):
    """Handle when user sends contact details"""
    lead_info = extract_full_lead_info(text)
    
    if lead_info.get("phone"):
        save_complete_order(sender_id, ad_id, lead_info, products_context)
        
        confirm_msg = f"Thank you! Order confirmed. {lead_info.get('phone')} ekata call karala delivery arrange karanawa 😊\n\nDear 💙"
        send_message(sender_id, confirm_msg, page_token)
        save_message(sender_id, ad_id, "assistant", confirm_msg)
        
        # Clear flow
        if sender_id in user_states:
            del user_states[sender_id]
    else:
        retry_msg = "Phone number eka ewanna puluwanda dear? 😊\n\nDear 💙"
        send_message(sender_id, retry_msg, page_token)
        save_message(sender_id, ad_id, "assistant", retry_msg)


def extract_full_lead_info(text):
    """Extract contact details from text"""
    info = {}

    # Phone
    phone_patterns = [
        r"(0\d{9})",
        r"(\+94\d{9})",
        r"(94\d{9})",
    ]
    for pattern in phone_patterns:
        match = re.search(pattern, text.replace(" ", "").replace("-", ""))
        if match:
            info["phone"] = match.group(1)
            break

    # Quantity
    qty_patterns = [
        r"(?:qty|quantity|keeyek)[:\s]*(\d+)",
        r"(\d+)\s*(?:ekak|ganna|layer|tier)",
    ]
    for pattern in qty_patterns:
        match = re.search(pattern, text.lower())
        if match:
            info["quantity"] = match.group(1)
            break

    # Name
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if re.match(r"^[A-Z][a-z]+(\s+[A-Z][a-z]+)+$", line):
            info["name"] = line[:50]
            break
        elif i == 0 and not re.search(r'\d{9}', line):
            if re.match(r"^[A-Z][a-z]+", line):
                info["name"] = line[:50]
                break

    # Address
    address_lines = []
    for i, line in enumerate(lines):
        if any(ind in line.lower() for ind in ["no:", "no.", "road", "street", "colombo", "kandy", "galle"]):
            address_lines.append(line)
        elif i > 0 and not re.search(r'\d{9,10}', line.replace(" ", "")) and len(line) > 5:
            if not re.match(r"^\d+$", line):
                address_lines.append(line)

    if address_lines:
        info["address"] = " ".join(address_lines)[:200]

    return info


def save_complete_order(sender_id, ad_id, lead_info, products_context):
    """Save order to Leads sheet"""
    try:
        sheet = get_sheet()
        if not sheet:
            return

        leads_sheet = sheet.worksheet("Leads")

        product_name = "Order Placed"
        if products_context:
            lines = products_context.split("\n")
            for line in lines:
                if line.strip() and " - " in line:
                    product_name = line.strip().split(" - ")[0][:50]
                    break

        if lead_info.get("quantity"):
            product_name = f"{product_name} (Qty: {lead_info['quantity']})"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        leads_sheet.append_row([
            sender_id,
            ad_id or "",
            lead_info.get("name", ""),
            lead_info.get("address", ""),
            lead_info.get("phone", ""),
            product_name,
            timestamp,
            "ordered",
        ])

        print(f"Saved order: {lead_info}", flush=True)

    except Exception as e:
        print(f"Error saving order: {e}", flush=True)


# =========================
# AI response generation
# =========================

def get_ai_response(user_message, history, products_context, product_images, sender_id, ad_id):
    """Generate natural AI response with context awareness"""
    try:
        # Build context-aware system prompt
        system_prompt = """You are a friendly sales assistant for Social Mart Sri Lanka. You chat in casual Singlish (mix of Sinhala and English), like a real Sri Lankan would text.

PERSONALITY:
- Warm, friendly, helpful (like talking to a friend)
- Use natural Singlish: "ow dear", "thiyanawa", "kamathi dha", "mehenna"
- Always end with "Dear 💙"
- Keep responses SHORT (1-3 sentences max)
- Sound natural and human, not robotic

CRITICAL RULES:
1. If user asks about products ("mona products", "thiyanawada", etc.): List products from the Products section below and suggest "SEND_IMAGES" + "START_LOCATION_FLOW"
2. If user asks for photos/pics/images: Reply naturally + add "SEND_IMAGES" tag
3. If user asks price ("kiyada", "mila"): Give exact price from Products list
4. If user asks details ("visthara denna", "warranty", "size"): Give detailed info from Products list
5. If user asks about delivery/COD: "Delivery Rs.350, 3-5 days. COD thiyanawa dear!"
6. For greetings: Respond warmly and ask how you can help
7. NEVER say "product list eka denna ba" if products exist below
8. NEVER repeat same message - vary your responses

CONVERSATION FLOW:
- If products shown → naturally suggest: "Location eka kohada?" (add START_LOCATION_FLOW tag)
- Answer all questions naturally without breaking conversation
- Don't be pushy, be helpful

"""

        if products_context:
            system_prompt += f"\nAVAILABLE PRODUCTS:\n{products_context}\n"
            system_prompt += "\nRemember: ONLY mention these products. Use EXACT prices."
        else:
            system_prompt += "\nNO PRODUCTS: Politely say products not available now, contact us later."

        # Add special instructions for current flow
        if sender_id in user_states:
            step = user_states[sender_id].get("step")
            if step == "collect_details":
                system_prompt += "\n\nUSER IS GIVING DETAILS: Acknowledge warmly and ask for any missing: name, address, phone."

        # Build messages
        messages = [{"role": "system", "content": system_prompt}]

        # Add history
        for msg in history[-8:]:
            messages.append({"role": msg["role"], "content": msg["message"]})

        messages.append({"role": "user", "content": user_message})

        # Call OpenAI
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=150,
            temperature=0.8,
        )

        reply = response.choices[0].message.content.strip()

        # Ensure "Dear 💙" ending
        if not reply.endswith("Dear 💙") and "Dear 💙" not in reply:
            reply = reply + "\n\nDear 💙"

        return reply

    except Exception as e:
        print(f"OpenAI error: {e}", flush=True)
        return "Sorry dear, technical issue ekak. Try again karanna puluwanda?\n\nDear 💙"


# =========================
# Product data from sheets
# =========================

def get_products_for_ad(ad_id):
    """Get products and images for ad"""
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

                for i in range(1, 6):
                    name_key = f"product_{i}_name"
                    price_key = f"product_{i}_price"

                    if row.get(name_key):
                        products_text += f"{row[name_key]} - {row.get(price_key, '')}\n"

                        # Get all 3 images per product
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
    """Send image via Messenger"""
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
                    "is_reusable": True,
                },
            }
        },
    }

    r = requests.post(url, params=params, json=payload)
    print(f"Send image: {r.status_code}", flush=True)


def search_products_by_query(query):
    """Search products using AI keywords"""
    try:
        keyword_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Extract product search keywords. Return only keywords, comma separated."},
                {"role": "user", "content": query},
            ],
            max_tokens=30,
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

                    if prod_name and prod_name not in [p["name"] for p in found_products]:
                        found_products.append({"name": prod_name, "price": prod_price})

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


# ====================
# Conversation logging
# ====================

def save_message(sender_id, ad_id, role, message):
    """Save to Conversations sheet"""
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
            message,
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

        return [
            {"role": m["role"], "message": m["message"]}
            for m in user_messages
            if m["role"] in ["user", "assistant"]
        ]

    except Exception as e:
        print(f"Error getting history: {e}", flush=True)
        return []


def get_user_ad_id(sender_id):
    """Get ad_id for user"""
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
        "message": {"text": text},
    }

    r = requests.post(url, params=params, json=payload)
    print(f"Send message: {r.status_code}", flush=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
