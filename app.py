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
    """Main message handler with intent understanding"""
    try:
        ad_id = get_user_ad_id(sender_id)
        save_message(sender_id, ad_id, "user", text)

        # Get products for ad if available
        products_context, product_images = get_products_for_ad(ad_id) if ad_id else (None, [])
        
        # If no products for ad, search ALL products by query
        if not products_context:
            products_context, product_images = search_products_by_query(text)
            print(f"Searched products, found: {bool(products_context)}", flush=True)
        
        # Get longer history for better context
        history = get_conversation_history(sender_id, limit=30)
        
        # Check if user is sending complete contact details
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
                
                combined_msg = "Hari! Delivery Rs.350. Order kamathi dha?\n\nDear 💙"
                send_message(sender_id, combined_msg, page_token)
                save_message(sender_id, ad_id, "assistant", combined_msg)
                return
            
            # Order confirmation step
            elif step == "ask_order":
                wants_order = check_agreement(text)
                
                if wants_order:
                    user_states[sender_id]["step"] = "collect_details"
                    details_msg = "Super! Name, address, phone ewanna.\n\nDear 💙"
                    send_message(sender_id, details_msg, page_token)
                    save_message(sender_id, ad_id, "assistant", details_msg)
                    return
                else:
                    # DON'T end with "Hari dear" - ask again
                    retry_msg = "Prashna thiyanawada dear? Order karanna kamathi nam mata kiyanna.\n\nDear 💙"
                    send_message(sender_id, retry_msg, page_token)
                    save_message(sender_id, ad_id, "assistant", retry_msg)
                    # Keep in same step
                    return
            
            # Collecting details
            elif step in ["collect_details", "collect_details_direct"]:
                lead_info = extract_full_lead_info(text)
                if lead_info.get("phone"):
                    handle_contact_details(sender_id, text, page_token, ad_id, products_context)
                    return

        # INTENT DETECTION - understand what user wants
        intent = detect_intent(text, history)
        print(f"🎯 Detected intent: {intent}", flush=True)

        # Handle specific intents
        if intent == "details":
            handle_details_request(sender_id, products_context, page_token, ad_id)
            return
        elif intent == "height":
            handle_height_request(sender_id, products_context, page_token, ad_id)
            return
        elif intent == "how_to_order":
            handle_how_to_order(sender_id, page_token, ad_id)
            return

        # Use AI for natural conversation
        reply = get_ai_response(text, history, products_context, product_images, sender_id, ad_id)
        
        # STRICT validation - reject hallucinations
        validation_result = validate_reply_strict(reply, products_context, text)
        if not validation_result["valid"]:
            print(f"❌ Invalid reply: {validation_result['reason']}", flush=True)
            reply = get_fallback_response(text, products_context, intent)
        
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
        send_message(sender_id, "Sorry dear, issue ekak.\n\nDear 💙", page_token)


# ======================
# Intent detection
# ======================

def detect_intent(text, history):
    """Detect user intent from message"""
    text_lower = text.lower()
    
    # Details/Visthara request
    details_keywords = ["details", "visthara", "visthara denna", "thawa visthara", 
                       "mata visthara", "more info", "info", "specification"]
    if any(kw in text_lower for kw in details_keywords):
        return "details"
    
    # Height/Size request
    height_keywords = ["height", "uchayak", "height eka", "uyathai", "size", 
                      "dimensions", "kiyada height"]
    if any(kw in text_lower for kw in height_keywords):
        return "height"
    
    # How to order
    order_keywords = ["kohamada order", "kohomada order", "order karanne kohomada",
                     "how to order", "order karanna", "order karanawada"]
    if any(kw in text_lower for kw in order_keywords):
        return "how_to_order"
    
    # Product list request
    products_keywords = ["mona products", "products mona", "thiyanawada"]
    if any(kw in text_lower for kw in products_keywords):
        return "product_list"
    
    return "general"


def handle_details_request(sender_id, products_context, page_token, ad_id):
    """Handle when user asks for details/visthara"""
    if products_context:
        msg = f"Mehenna details!\n\n{products_context}\n\nOrder kamathi dha?\n\nDear 💙"
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)
    else:
        msg = "Details nehe dear, mata message karanna.\n\nDear 💙"
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)


def handle_height_request(sender_id, products_context, page_token, ad_id):
    """Handle when user asks for height"""
    if products_context:
        # Extract height from details if available
        if "height" in products_context.lower() or "cm" in products_context.lower():
            msg = f"Height details:\n\n{products_context}\n\nOrder kamathi dha?\n\nDear 💙"
        else:
            msg = f"Product details:\n\n{products_context}\n\nOrder kamathi dha?\n\nDear 💙"
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)
    else:
        msg = "Height details nehe dear.\n\nDear 💙"
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)


def handle_how_to_order(sender_id, page_token, ad_id):
    """Handle 'how to order' questions"""
    msg = "Order karanna:\n1. Product select karanna\n2. Location ewanna\n3. Name, address, phone ewanna\n\nMata message karanna dear!\n\nDear 💙"
    send_message(sender_id, msg, page_token)
    save_message(sender_id, ad_id, "assistant", msg)


def get_fallback_response(text, products_context, intent):
    """Fallback response when AI fails validation"""
    if intent == "product_list" or "mona" in text.lower():
        if products_context:
            return f"Mehenna products:\n\n{products_context}\n\nOrder kamathi dha? SEND_IMAGES START_LOCATION_FLOW\n\nDear 💙"
        else:
            return "Products nehe dear, mata message karanna.\n\nDear 💙"
    
    if "thiyanawadha" in text.lower() or "thiyanawada" in text.lower():
        if products_context:
            return f"Ow thiyanawa dear!\n\n{products_context}\n\nOrder kamathi dha? SEND_IMAGES START_LOCATION_FLOW\n\nDear 💙"
        else:
            return "Nehe dear, eka nehe.\n\nDear 💙"
    
    return "Mata message karanna dear!\n\nDear 💙"


def validate_reply_strict(reply, products_context, user_message):
    """STRICT validation to prevent hallucinations"""
    
    # Forbidden words - products we DON'T have
    forbidden_words = [
        "fridge", "refrigerator", "samsung", "lg", "microwave",
        "gas cooker", "200l", "frost-free", "5 star", "warranty"
    ]
    
    for word in forbidden_words:
        if word in reply.lower():
            # Check if this word is actually in products_context
            if not products_context or word not in products_context.lower():
                return {
                    "valid": False,
                    "reason": f"Mentioned forbidden product: {word}"
                }
    
    # Check for product patterns
    product_patterns = [
        r'(\d+)\s*(?:Tier|Layer)',
        r'(?:Stainless Steel|Wooden|Metal|Plastic)',
        r'Rs\.?\s*\d+[,\d]*',
        r'රු\.?\s*\d+[,\d]*',
    ]
    
    for pattern in product_patterns:
        matches = re.findall(pattern, reply, re.IGNORECASE)
        if matches:
            if products_context:
                for match in matches:
                    if str(match).lower() not in products_context.lower():
                        return {
                            "valid": False,
                            "reason": f"Hallucinated: {match}"
                        }
            else:
                return {
                    "valid": False,
                    "reason": "Mentioned products when none available"
                }
    
    # Check yes/no questions
    if "thiyanawadha" in user_message.lower() or "thiyanawada" in user_message.lower():
        if "ow" not in reply.lower() and "nehe" not in reply.lower() and "නැහැ" not in reply and "ඔව්" not in reply:
            return {
                "valid": False,
                "reason": "Didn't answer yes/no question"
            }
    
    # Never say "Price on request"
    if "price on request" in reply.lower() or "request" in reply.lower():
        return {
            "valid": False,
            "reason": "Hallucinated 'price on request'"
        }
    
    # Don't end conversation prematurely
    if reply.strip() == "Hari dear!\n\nDear 💙":
        return {
            "valid": False,
            "reason": "Ending conversation without asking for order"
        }
    
    return {"valid": True, "reason": ""}


# -------------
# Helper functions
# -------------

def detect_contact_details(text):
    """Detect if message contains phone number + other details"""
    has_phone = bool(re.search(r'0\d{9}|94\d{9}|\+94\d{9}', text.replace(' ', '').replace('-', '')))
    
    address_indicators = ['no:', 'no.', 'road', 'street', 'colombo', 'kandy', 'galle', 'negombo', 'kurunegala', 'matara', 'anuradhapura']
    has_address = any(indicator in text.lower() for indicator in address_indicators)
    
    has_name = bool(re.search(r'[A-Z][a-z]+\s+[A-Z][a-z]+', text))
    
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
        
        confirm_msg = f"Thanks! {lead_info.get('phone')} ekata call karanawa.\n\nDear 💙"
        send_message(sender_id, confirm_msg, page_token)
        save_message(sender_id, ad_id, "assistant", confirm_msg)
        
        if sender_id in user_states:
            del user_states[sender_id]
    else:
        retry_msg = "Phone number ewanna.\n\nDear 💙"
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

        print(f"✅ Saved order: {lead_info}", flush=True)

    except Exception as e:
        print(f"Error saving order: {e}", flush=True)


# =========================
# AI response generation
# =========================

def get_ai_response(user_message, history, products_context, product_images, sender_id, ad_id):
    """Generate natural AI response with strict rules"""
    try:
        system_prompt = """You are a friendly sales assistant for Social Mart Sri Lanka.

CRITICAL LANGUAGE RULES:
1. Use SIMPLE SINGLISH (2-4 words per sentence)
2. Mix Sinhala/English naturally: "ow dear", "thiyanawa", "nehe"
3. Keep responses SHORT (1-2 sentences)
4. Always end with "Dear 💙"

CRITICAL PRODUCT RULES - NEVER BREAK:
1. ONLY mention products in "AVAILABLE PRODUCTS" below
2. Use EXACT names and prices from the list
3. If asked about product NOT in list → say "Nehe dear, eka nehe"
4. NEVER invent: Fridge, Samsung, LG, Microwave, Gas Cooker, 200L, Frost-free
5. NEVER say "Price on request"

ANSWER QUESTIONS DIRECTLY:
- Details/Visthara → Give full product details from list
- Height → Give height info if available in details
- "X thiyanawadha?" → Answer "Ow thiyanawa" or "Nehe dear"
- "Kohamada order karanai" → Explain: location ewanna, then details ewanna

NEVER END WITH "Hari dear!" - Always ask: "Order kamathi dha?"

"""

        if products_context:
            system_prompt += f"\nAVAILABLE PRODUCTS (ONLY these exist):\n{products_context}\n"
            system_prompt += "\n⚠️ CRITICAL: NEVER mention products not in this exact list!"
        else:
            system_prompt += "\nNO PRODUCTS AVAILABLE. Say: 'Products nehe dear, mata message karanna.'"

        messages = [{"role": "system", "content": system_prompt}]

        # Add more history for context
        for msg in history[-12:]:
            messages.append({"role": msg["role"], "content": msg["message"]})

        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=80,
            temperature=0.4,
        )

        reply = response.choices[0].message.content.strip()

        if not reply.endswith("Dear 💙") and "Dear 💙" not in reply:
            reply = reply + "\n\nDear 💙"

        return reply

    except Exception as e:
        print(f"OpenAI error: {e}", flush=True)
        return "Sorry dear, issue ekak.\n\nDear 💙"


# =========================
# Product data from sheets
# =========================

def get_products_for_ad(ad_id):
    """Get products and images for specific ad"""
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
                    details_key = f"product_{i}_details"

                    if row.get(name_key):
                        product_line = f"{row[name_key]} - {row.get(price_key, '')}"
                        
                        details = row.get(details_key, "")
                        if details:
                            product_line += f"\n{details}"
                        
                        products_text += product_line + "\n\n"

                        for img_num in range(1, 4):
                            image_key = f"product_{i}_image_{img_num}"
                            if row.get(image_key):
                                img_url = row[image_key]
                                if img_url and img_url.startswith("http"):
                                    image_urls.append(img_url)

                return products_text.strip(), image_urls

        return None, []

    except Exception as e:
        print(f"Error getting products: {e}", flush=True)
        return None, []


def search_products_by_query(query):
    """Search ALL products in sheet by query"""
    try:
        sheet = get_sheet()
        if not sheet:
            return None, []

        ad_products_sheet = sheet.worksheet("Ad_Products")
        records = ad_products_sheet.get_all_records()

        query_lower = query.lower()
        keywords = re.findall(r'\w+', query_lower)

        found_products = []
        found_images = []

        for row in records:
            for i in range(1, 6):
                name = str(row.get(f"product_{i}_name", "")).lower()
                
                if name:
                    if any(kw in name for kw in keywords if len(kw) > 2):
                        prod_name = row.get(f"product_{i}_name")
                        prod_price = row.get(f"product_{i}_price")
                        prod_details = row.get(f"product_{i}_details", "")

                        if prod_name and prod_name not in [p["name"] for p in found_products]:
                            found_products.append({
                                "name": prod_name,
                                "price": prod_price,
                                "details": prod_details
                            })

                            for img_num in range(1, 4):
                                img_url = row.get(f"product_{i}_image_{img_num}")
                                if img_url and img_url.startswith("http"):
                                    found_images.append(img_url)

        if found_products:
            products_text = ""
            for prod in found_products[:5]:
                products_text += f"{prod['name']} - {prod['price']}"
                if prod['details']:
                    products_text += f"\n{prod['details']}"
                products_text += "\n\n"

            print(f"✅ Found {len(found_products)} products", flush=True)
            return products_text.strip(), found_images[:15]

        # No keyword matches - return ALL products
        all_products_text = ""
        all_images = []
        for row in records:
            for i in range(1, 6):
                name = row.get(f"product_{i}_name")
                if name:
                    price = row.get(f"product_{i}_price")
                    details = row.get(f"product_{i}_details", "")
                    
                    all_products_text += f"{name} - {price}"
                    if details:
                        all_products_text += f"\n{details}"
                    all_products_text += "\n\n"
                    
                    for img_num in range(1, 4):
                        img_url = row.get(f"product_{i}_image_{img_num}")
                        if img_url and img_url.startswith("http"):
                            all_images.append(img_url)

        if all_products_text:
            print(f"ℹ️ Returning ALL products", flush=True)
            return all_products_text.strip(), all_images[:15]

        return None, []

    except Exception as e:
        print(f"Error in search: {e}", flush=True)
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


def get_conversation_history(sender_id, limit=30):
    """Get conversation history - now supports 30 messages"""
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
