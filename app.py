import os
import requests
import json
import re
from flask import Flask, request
from openai import OpenAI
import httpx
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import time

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

# Initialize OpenAI with timeout and retry settings
client = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=httpx.Timeout(30.0, connect=10.0),
    max_retries=2
)

# User conversation state tracking
user_states = {}

# =====================
# CACHING SYSTEM
# =====================

products_cache = {
    "data": None,
    "timestamp": 0,
    "ttl": 300
}

conversation_cache = {}
CONVERSATION_CACHE_TTL = 60

# =====================
# EVENT DEDUPLICATION - NEW!
# =====================

processed_events = {}
EVENT_CACHE_TTL = 300  # 5 minutes


def get_cached_products():
    """Get products from cache or fetch if expired"""
    current_time = time.time()
    
    if products_cache["data"] and (current_time - products_cache["timestamp"]) < products_cache["ttl"]:
        print("✅ Using cached products", flush=True)
        return products_cache["data"]
    
    print("📥 Fetching fresh products from sheet", flush=True)
    sheet = get_sheet()
    if not sheet:
        return None
    
    try:
        ad_products_sheet = sheet.worksheet("Ad_Products")
        records = ad_products_sheet.get_all_records()
        
        products_cache["data"] = records
        products_cache["timestamp"] = current_time
        
        print(f"✅ Cached {len(records)} product rows", flush=True)
        return records
    except Exception as e:
        print(f"Error fetching products: {e}", flush=True)
        return products_cache["data"]


def get_cached_conversation_history(sender_id, limit=30):
    """Get conversation history with caching"""
    current_time = time.time()
    cache_key = f"{sender_id}_{limit}"
    
    if cache_key in conversation_cache:
        cached_data, cached_time = conversation_cache[cache_key]
        if (current_time - cached_time) < CONVERSATION_CACHE_TTL:
            print(f"✅ Using cached history for {sender_id}", flush=True)
            return cached_data
    
    print(f"📥 Fetching fresh history for {sender_id}", flush=True)
    history = get_conversation_history_from_sheet(sender_id, limit)
    
    conversation_cache[cache_key] = (history, current_time)
    
    return history


def clear_conversation_cache(sender_id):
    """Clear cache for a specific user when new message arrives"""
    keys_to_delete = [k for k in conversation_cache.keys() if k.startswith(sender_id)]
    for key in keys_to_delete:
        del conversation_cache[key]


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


# =====================
# Context Memory System
# =====================

def get_user_context(sender_id):
    """Get or create user context memory"""
    if sender_id not in user_states:
        user_states[sender_id] = {
            "step": None,
            "ad_id": None,
            "product": None,
            "product_name": None,
            "location": None,
            "phone": None,
            "name": None,
            "address": None,
            "last_topic": None,
            "asked_location": False,
            "asked_order": False,
            "order_retry_count": 0,
            "collecting_name": False,
            "collecting_address": False,
            "collecting_phone": False,
        }
    return user_states[sender_id]


def update_user_context(sender_id, **kwargs):
    """Update user context with new information"""
    context = get_user_context(sender_id)
    context.update(kwargs)
    user_states[sender_id] = context
    print(f"💾 Updated context: step={context.get('step')}, product={context.get('product_name')}", flush=True)


def extract_context_from_history(sender_id):
    """Extract context from conversation history"""
    history = get_cached_conversation_history(sender_id, limit=10)
    context = get_user_context(sender_id)
    
    for msg in reversed(history):
        if msg["role"] == "user":
            product = extract_product_from_query(msg["message"])
            if product and not context.get("product_name"):
                update_user_context(sender_id, product_name=product, last_topic=product)
                break
    
    for msg in reversed(history):
        if msg["role"] == "user":
            if is_valid_location(msg["message"]) and not context.get("location"):
                update_user_context(sender_id, location=msg["message"])
                break


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
                    
                    # =====================
                    # DEDUPLICATION LOGIC - PREVENTS DUPLICATE MESSAGES
                    # =====================
                    event_id = None
                    if event.get("message"):
                        event_id = event["message"].get("mid")
                    elif event.get("referral"):
                        event_id = f"ref_{sender_id}_{event['referral'].get('ref')}_{event.get('timestamp')}"
                    
                    if event_id:
                        current_time = time.time()
                        
                        # Check if already processed
                        if event_id in processed_events:
                            if (current_time - processed_events[event_id]) < EVENT_CACHE_TTL:
                                print(f"⚠️ Skipping duplicate event: {event_id}", flush=True)
                                continue
                        
                        # Mark as processed
                        processed_events[event_id] = current_time
                        
                        # Clean old events from cache
                        processed_events_copy = dict(processed_events)
                        for old_id, old_time in processed_events_copy.items():
                            if (current_time - old_time) > EVENT_CACHE_TTL:
                                del processed_events[old_id]
                    
                    # Handle referral (initial ad click) - ONLY if no message present
                    if "referral" in event and "message" not in event:
                        ad_id = event["referral"].get("ref")
                        handle_ad_referral(sender_id, ad_id, page_token)
                    
                    # Handle regular messages
                    elif event.get("message") and "text" in event["message"]:
                        text = event["message"]["text"]
                        print(f"Message from {sender_id}: {text}", flush=True)
                        
                        clear_conversation_cache(sender_id)
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

        update_user_context(sender_id, step="ask_location", ad_id=ad_id, product=products_context)

        if products_context:
            product_message = f"Mehenna ape products:\n\n{products_context}"
            send_message(sender_id, product_message, page_token)
            save_message(sender_id, ad_id, "assistant", product_message)

        if product_images:
            for img_url in product_images[:10]:
                send_image(sender_id, img_url, page_token)
                time.sleep(0.3)

        location_msg = "Location eka kohada?\n\nDear 💙"
        send_message(sender_id, location_msg, page_token)
        save_message(sender_id, ad_id, "assistant", location_msg)
        
        update_user_context(sender_id, asked_location=True)

        print(f"Ad referral: sender={sender_id}, ad_id={ad_id}", flush=True)
    except Exception as e:
        print(f"Error in handle_ad_referral: {e}", flush=True)


def handle_message(sender_id, text, page_token):
    """Main message handler with AI-POWERED INTENT DETECTION"""
    try:
        context = get_user_context(sender_id)
        ad_id = context.get("ad_id") or get_user_ad_id(sender_id)
        
        save_message(sender_id, ad_id, "user", text)

        # Get products
        products_context, product_images = get_products_for_ad(ad_id) if ad_id else (None, [])
        
        if not products_context:
            products_context, product_images = get_all_products()
            print(f"Using ALL products", flush=True)
        
        if not context.get("product_name"):
            extract_context_from_history(sender_id)
            context = get_user_context(sender_id)
        
        current_product = extract_product_from_query(text)
        if current_product:
            update_user_context(sender_id, product_name=current_product, last_topic=current_product)
        
        if is_valid_location(text) and not context.get("location"):
            update_user_context(sender_id, location=text)
            print(f"📍 Saved location: {text}", flush=True)
        
        history = get_cached_conversation_history(sender_id, limit=30)
        
        # STRUCTURED CONTACT COLLECTION
        if context.get("step") == "collect_name":
            update_user_context(sender_id, name=text, step="collect_address")
            msg = "Address eka ewanna dear.\n\nDear 💙"
            send_message(sender_id, msg, page_token)
            save_message(sender_id, ad_id, "assistant", msg)
            return
        
        if context.get("step") == "collect_address":
            update_user_context(sender_id, address=text, step="collect_phone")
            msg = "Phone number ewanna dear.\n\nDear 💙"
            send_message(sender_id, msg, page_token)
            save_message(sender_id, ad_id, "assistant", msg)
            return
        
        if context.get("step") == "collect_phone":
            phone = extract_phone_number(text)
            if phone:
                update_user_context(sender_id, phone=phone)
                
                # Save order
                lead_info = {
                    "name": context.get("name", ""),
                    "address": context.get("address", ""),
                    "phone": phone
                }
                save_complete_order(sender_id, ad_id, lead_info, products_context)
                
                # Thank you message
                thank_msg = f"Thank you dear! {phone} ekata call karanawa soon.\n\nDear 💙"
                send_message(sender_id, thank_msg, page_token)
                save_message(sender_id, ad_id, "assistant", thank_msg)
                
                # Reset state
                if sender_id in user_states:
                    del user_states[sender_id]
                return
            else:
                msg = "Phone number ekak ewanna (Example: 0771234567)\n\nDear 💙"
                send_message(sender_id, msg, page_token)
                save_message(sender_id, ad_id, "assistant", msg)
                return
        
        # Check if user is sending complete contact details (old method)
        if detect_contact_details(text):
            handle_contact_details(sender_id, text, page_token, ad_id, products_context)
            return

        # AI-POWERED INTENT DETECTION
        intent_data = detect_intent_with_ai(text, history, context, products_context)
        intent = intent_data["intent"]
        confidence = intent_data["confidence"]
        entities = intent_data["entities"]
        
        print(f"🤖 AI Intent: {intent} (confidence: {confidence}), Entities: {entities}", flush=True)

        # Handle specific intents
        if intent == "product_availability":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_availability_request(sender_id, text, products_context, product_images, page_token, ad_id, context, entities)
            return
        elif intent == "photos":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_photo_request(sender_id, text, products_context, product_images, page_token, ad_id, context, entities)
            return
        elif intent == "delivery":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_delivery_request(sender_id, page_token, ad_id, context)
            return
        elif intent == "details":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_details_request(sender_id, text, products_context, product_images, page_token, ad_id, context, entities)
            return
        elif intent == "dimensions":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_dimensions_request(sender_id, text, products_context, page_token, ad_id, context, entities)
            return
        elif intent == "price_inquiry":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_price_inquiry(sender_id, text, products_context, page_token, ad_id, context, entities)
            return
        elif intent == "total_price":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_total_price_inquiry(sender_id, text, products_context, page_token, ad_id, context, entities)
            return
        elif intent == "product_list":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_product_list_request(sender_id, products_context, product_images, page_token, ad_id, context)
            return
        elif intent == "how_to_order":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_how_to_order(sender_id, page_token, ad_id)
            return

        # Flow management
        step = context.get("step")
        
        if step == "ask_location":
            if is_valid_location(text):
                update_user_context(sender_id, location=text, step="ask_order")
                
                msg1 = "Hari! Delivery Rs.350.\n\nDear 💙"
                send_message(sender_id, msg1, page_token)
                save_message(sender_id, ad_id, "assistant", msg1)
                
                time.sleep(1)
                
                msg2 = "Order kamathi dha?\n\nDear 💙"
                send_message(sender_id, msg2, page_token)
                save_message(sender_id, ad_id, "assistant", msg2)
                
                update_user_context(sender_id, asked_order=True)
                return
            else:
                update_user_context(sender_id, step=None)
        
        elif step == "ask_order":
            wants_order = check_agreement(text)
            
            if intent in ["product_availability", "photos", "details", "price_inquiry", "product_list", "dimensions", "total_price"]:
                update_user_context(sender_id, step=None, order_retry_count=0)
                # Re-route to appropriate handler
                return handle_message(sender_id, text, page_token)
            
            if wants_order:
                # Start structured collection
                update_user_context(sender_id, step="collect_name", order_retry_count=0)
                details_msg = "Name eka ewanna dear.\n\nDear 💙"
                send_message(sender_id, details_msg, page_token)
                save_message(sender_id, ad_id, "assistant", details_msg)
                return
            else:
                retry_count = context.get("order_retry_count", 0)
                
                if retry_count >= 2:
                    print(f"⚠️ Too many retries, clearing state", flush=True)
                    update_user_context(sender_id, step=None, order_retry_count=0)
                    
                    msg = "Mata message karanna dear, help karannam!\n\nDear 💙"
                    send_message(sender_id, msg, page_token)
                    save_message(sender_id, ad_id, "assistant", msg)
                    return
                
                update_user_context(sender_id, order_retry_count=retry_count + 1)
                retry_msg = "Ow kiyanna sir/madam.\n\nDear 💙"
                send_message(sender_id, retry_msg, page_token)
                save_message(sender_id, ad_id, "assistant", retry_msg)
                return

        # Use AI for general conversation
        reply = get_ai_response(text, history, products_context, product_images, sender_id, ad_id, context)
        
        validation_result = validate_reply_strict(reply, products_context, text)
        if not validation_result["valid"]:
            print(f"❌ Invalid reply: {validation_result['reason']}", flush=True)
            reply = get_fallback_response(text, products_context, intent)
        
        if "SEND_IMAGES" in reply:
            reply = reply.replace("SEND_IMAGES", "").strip()
            if product_images:
                for img_url in product_images[:10]:
                    send_image(sender_id, img_url, page_token)
                    time.sleep(0.3)
        
        if "START_LOCATION_FLOW" in reply:
            reply = reply.replace("START_LOCATION_FLOW", "").strip()
            if not context.get("asked_location"):
                update_user_context(sender_id, step="ask_location", asked_location=True)
        
        send_message(sender_id, reply, page_token)
        save_message(sender_id, ad_id, "assistant", reply)

    except Exception as e:
        print(f"Error in handle_message: {e}", flush=True)
        send_message(sender_id, "Sorry dear, issue ekak.\n\nDear 💙", page_token)


# ======================
# AI-POWERED INTENT DETECTION - ENHANCED
# ======================

def detect_intent_with_ai(user_message, history, context, products_context):
    """Use OpenAI to detect user intent - ULTRA SMART!"""
    try:
        context_info = ""
        if context.get("product_name"):
            context_info += f"User was talking about: {context['product_name']}\n"
        if context.get("location"):
            context_info += f"User location: {context['location']}\n"
        
        recent_history = ""
        if history:
            for msg in history[-2:]:
                recent_history += f"{msg['role']}: {msg['message']}\n"
        
        prompt = f"""You are an intent classifier for a Sri Lankan e-commerce chatbot.

AVAILABLE INTENTS:
1. product_availability - User asking if product exists/available (thiyanawada, available, stock, ithiri)
2. photos - User wants images (photo, photos, pics, pictures, image, foto, 4to, pintura, පින්තූර, ewanna, dana)
3. delivery - User asking delivery charges (delivery, courier, charges, chargers, කරවන්න, delivery eka)
4. details - User wants full specifications (details, visthara, specification, විස්තර, info, more info)
5. dimensions - User asking specific measurements (height, width, size, adi, uchayak, usa, uyathai, dimensions, උස, පළල)
6. price_inquiry - User asking price only (how much, kiyada, gana, ganang, price, ගාන, කීයද, ගාන කීයද)
7. total_price - User asking total with delivery (sampura gana, total, total eka, sampura, සම්පූර්ණ ගාන)
8. product_list - User asking what products available (mona products, products mona, kohomada, මොනවද)
9. how_to_order - User asking how to place order
10. greeting - User says hello, hi, ayubowan
11. agreement - User says yes, ow, ok, kamathi (ඔව්, හරි, කැමති)
12. disagreement - User says no, nehe, epa (නැහැ, එපා)
13. general - Everything else

PRODUCTS AVAILABLE:
{products_context[:500] if products_context else "No products"}

CONVERSATION CONTEXT:
{context_info}

RECENT CHAT:
{recent_history}

USER MESSAGE: "{user_message}"

Respond in JSON:
{{
  "intent": "intent_name",
  "confidence": 0.0-1.0,
  "entities": {{
    "product": "product name if mentioned",
    "quantity": "number if mentioned"
  }}
}}

Examples:
"mona products dha thiyanai" → {{"intent": "product_list", "confidence": 0.95, "entities": {{}}}}
"how much" → {{"intent": "price_inquiry", "confidence": 0.9, "entities": {{}}}}
"ගාන කීයද" → {{"intent": "price_inquiry", "confidence": 0.95, "entities": {{}}}}
"sampura gana" → {{"intent": "total_price", "confidence": 0.95, "entities": {{}}}}
"racks thiyanawada" → {{"intent": "product_availability", "confidence": 0.95, "entities": {{"product": "rack"}}}}
"photos ewanna" → {{"intent": "photos", "confidence": 0.95, "entities": {{}}}}
"4to dana" → {{"intent": "photos", "confidence": 0.9, "entities": {{}}}}
"delivery charges" → {{"intent": "delivery", "confidence": 0.95, "entities": {{}}}}
"height kiyada" → {{"intent": "dimensions", "confidence": 0.95, "entities": {{}}}}
"usa eka" → {{"intent": "dimensions", "confidence": 0.9, "entities": {{}}}}
"visthara denna" → {{"intent": "details", "confidence": 0.95, "entities": {{}}}}
"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an intent classification expert. Always respond with valid JSON."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.3,
            timeout=20
        )

        result = response.choices[0].message.content.strip()
        
        try:
            intent_data = json.loads(result)
            return intent_data
        except:
            print(f"Failed to parse JSON: {result}", flush=True)
            return {
                "intent": "general",
                "confidence": 0.5,
                "entities": {}
            }

    except (ConnectionError, TimeoutError, httpx.ConnectError, httpx.TimeoutException) as e:
        print(f"Intent detection connection error: {type(e).__name__} - {str(e)}", flush=True)
        return {
            "intent": "general",
            "confidence": 0.5,
            "entities": {}
        }
    except Exception as e:
        print(f"Intent detection error: {e}", flush=True)
        return {
            "intent": "general",
            "confidence": 0.5,
            "entities": {}
        }


def extract_product_from_query(text):
    """Extract specific product name from user query"""
    text_lower = text.lower()
    
    if "4 tier" in text_lower or "4tier" in text_lower or "4 layer" in text_lower or "four tier" in text_lower:
        return "4 tier"
    if "3 tier" in text_lower or "3tier" in text_lower or "3 layer" in text_lower or "three tier" in text_lower:
        return "3 tier"
    if "triangle" in text_lower:
        return "triangle"
    if "foldable" in text_lower:
        return "foldable"
    if "cloth rack" in text_lower:
        return "cloth rack"
    if "storage rack" in text_lower or "storage" in text_lower:
        return "storage"
    if "rack" in text_lower or "racks" in text_lower:
        return "rack"
    
    return None


def extract_phone_number(text):
    """Extract phone number from text"""
    phone_patterns = [
        r"(0\d{9})",
        r"(\+94\d{9})",
        r"(94\d{9})",
    ]
    for pattern in phone_patterns:
        match = re.search(pattern, text.replace(" ", "").replace("-", ""))
        if match:
            return match.group(1)
    return None


def is_valid_location(text):
    """Check if text is a valid Sri Lankan location"""
    locations = [
        "colombo", "kandy", "galle", "jaffna", "negombo", "matara", "kurunegala",
        "anuradhapura", "trincomalee", "batticaloa", "ratnapura", "badulla", "ampara",
        "hambantota", "kalutara", "kegalle", "kilinochchi", "mannar", "monaragala",
        "mullaitivu", "nuwara eliya", "polonnaruwa", "puttalam", "vavuniya",
        "homagama", "maharagama", "dehiwala", "mount lavinia", "moratuwa", "panadura",
        "nugegoda", "kotte", "kaduwela", "kelaniya", "wattala", "ja-ela",
        "road", "street", "lane", "town", "city", "gama", "watta"
    ]
    
    text_lower = text.lower()
    
    if any(loc in text_lower for loc in locations):
        return True
    
    if len(text.split()) <= 3 and not any(word in text_lower for word in ["delivery", "order", "price", "photo", "kamathi", "kiyada", "kohamada", "thiyanawada"]):
        return True
    
    return False


# ======================
# Context-aware handlers - ENHANCED
# ======================

def handle_total_price_inquiry(sender_id, user_text, products_context, page_token, ad_id, context, entities):
    """Handle 'sampura gana' - total price with delivery"""
    
    specific_product = entities.get("product") or extract_product_from_query(user_text) or context.get("product_name")
    
    if specific_product and products_context:
        # Extract price for specific product
        products_lines = products_context.split("\n")
        
        for line in products_lines:
            if " - Rs." in line and specific_product in line.lower():
                # Extract price
                price_match = re.search(r'Rs\.?\s*([\d,]+)', line)
                if price_match:
                    price_str = price_match.group(1).replace(',', '')
                    try:
                        product_price = int(price_str)
                        total_price = product_price + 350
                        
                        msg = f"{specific_product}:\nProduct: Rs.{product_price:,}\nDelivery: Rs.350\n━━━━━━━\nTotal: Rs.{total_price:,}\n\nDear 💙"
                        send_message(sender_id, msg, page_token)
                        save_message(sender_id, ad_id, "assistant", msg)
                        
                        if not context.get("asked_order"):
                            time.sleep(1)
                            msg2 = "Order kamathi dha?\n\nDear 💙"
                            send_message(sender_id, msg2, page_token)
                            save_message(sender_id, ad_id, "assistant", msg2)
                            update_user_context(sender_id, asked_order=True)
                        return
                    except:
                        pass
    
    # Generic response if no specific product
    msg = "Product price + Delivery Rs.350 = Total\n\nMata product name ekak ewanna, total eka kiyanna.\n\nDear 💙"
    send_message(sender_id, msg, page_token)
    save_message(sender_id, ad_id, "assistant", msg)


def handle_dimensions_request(sender_id, user_text, products_context, page_token, ad_id, context, entities):
    """Handle dimension requests - height, width, size"""
    
    specific_product = entities.get("product") or extract_product_from_query(user_text) or context.get("product_name")
    
    if products_context:
        if specific_product:
            # Filter for specific product
            filtered_details = []
            products_lines = products_context.split("\n")
            
            capturing = False
            for line in products_lines:
                if " - Rs." in line and specific_product in line.lower():
                    capturing = True
                    filtered_details.append(line)
                elif " - Rs." in line and capturing:
                    break
                elif capturing and line.strip():
                    filtered_details.append(line)
            
            if filtered_details:
                details_text = "\n".join(filtered_details)
                msg = f"Mehenna {specific_product} dimensions:\n\n{details_text}\n\nDear 💙"
            else:
                msg = f"Dimensions:\n\n{products_context}\n\nDear 💙"
        else:
            msg = f"Dimensions:\n\n{products_context}\n\nDear 💙"
        
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)
        
        if not context.get("asked_order"):
            time.sleep(1)
            msg2 = "Order kamathi dha?\n\nDear 💙"
            send_message(sender_id, msg2, page_token)
            save_message(sender_id, ad_id, "assistant", msg2)
            update_user_context(sender_id, asked_order=True)
    else:
        msg = "Dimensions nehe dear.\n\nDear 💙"
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)


def handle_price_inquiry(sender_id, user_text, products_context, page_token, ad_id, context, entities):
    """Handle 'how much', 'gana kiyada', price questions"""
    
    specific_product = entities.get("product") or extract_product_from_query(user_text) or context.get("product_name")
    
    if specific_product and products_context:
        filtered_products = []
        products_lines = products_context.split("\n")
        
        for line in products_lines:
            if " - Rs." in line and specific_product in line.lower():
                filtered_products.append(line)
        
        if filtered_products:
            msg = f"Mehenna {specific_product} price:\n\n" + "\n".join(filtered_products) + "\n\nDear 💙"
        else:
            msg = f"Mehenna prices:\n\n{products_context}\n\nDear 💙"
    elif products_context:
        msg = f"Mehenna prices:\n\n{products_context}\n\nDear 💙"
    else:
        msg = "Mata product name ekak ewanna, price kiyanna.\n\nDear 💙"
    
    send_message(sender_id, msg, page_token)
    save_message(sender_id, ad_id, "assistant", msg)
    
    if not context.get("asked_order"):
        time.sleep(1)
        msg2 = "Order kamathi dha?\n\nDear 💙"
        send_message(sender_id, msg2, page_token)
        save_message(sender_id, ad_id, "assistant", msg2)
        update_user_context(sender_id, asked_order=True)


def handle_product_list_request(sender_id, products_context, product_images, page_token, ad_id, context):
    """Handle 'mona products thiyanada' - show ALL products"""
    
    if not products_context:
        msg = "Mata minute ekak wait karanna, products load karanawa.\n\nDear 💙"
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)
        return
    
    msg = f"Mehenna ape products:\n\n{products_context}\n\nDear 💙"
    send_message(sender_id, msg, page_token)
    save_message(sender_id, ad_id, "assistant", msg)
    
    if product_images:
        time.sleep(0.5)
        for img_url in product_images[:10]:
            send_image(sender_id, img_url, page_token)
            time.sleep(0.3)
    
    if not context.get("asked_order"):
        time.sleep(1)
        msg2 = "Order kamathi dha?\n\nDear 💙"
        send_message(sender_id, msg2, page_token)
        save_message(sender_id, ad_id, "assistant", msg2)
        update_user_context(sender_id, asked_order=True)


def handle_availability_request(sender_id, user_text, products_context, product_images, page_token, ad_id, context, entities):
    """Handle 'thiyanawada' questions"""
    
    specific_product = entities.get("product") or extract_product_from_query(user_text) or context.get("product_name")
    
    if specific_product:
        searched_products, searched_images = search_products_by_query(specific_product)
        
        if searched_products:
            msg = f"Ow {specific_product} thiyanawa dear!\n\n{searched_products}\n\nDear 💙"
            send_message(sender_id, msg, page_token)
            save_message(sender_id, ad_id, "assistant", msg)
            
            if searched_images:
                time.sleep(0.5)
                for img_url in searched_images[:10]:
                    send_image(sender_id, img_url, page_token)
                    time.sleep(0.3)
            
            if not context.get("asked_order"):
                time.sleep(1)
                msg2 = "Order kamathi dha?\n\nDear 💙"
                send_message(sender_id, msg2, page_token)
                save_message(sender_id, ad_id, "assistant", msg2)
                update_user_context(sender_id, asked_order=True)
            return
        else:
            msg = f"Nehe dear, {specific_product} nehe.\n\nDear 💙"
            send_message(sender_id, msg, page_token)
            save_message(sender_id, ad_id, "assistant", msg)
            return
    
    if products_context:
        msg = f"Ow thiyanawa dear!\n\n{products_context}\n\nDear 💙"
    else:
        msg = "Mata product name ekak ewanna dear.\n\nDear 💙"
    
    send_message(sender_id, msg, page_token)
    save_message(sender_id, ad_id, "assistant", msg)
    
    if product_images:
        time.sleep(0.5)
        for img_url in product_images[:10]:
            send_image(sender_id, img_url, page_token)
            time.sleep(0.3)


def handle_photo_request(sender_id, user_text, products_context, product_images, page_token, ad_id, context, entities):
    """Handle photos - foto, 4to, pintura, pics"""
    
    specific_product = entities.get("product") or extract_product_from_query(user_text) or context.get("product_name")
    
    if specific_product and products_context:
        filtered_images = get_specific_product_images(specific_product, ad_id)
        
        if filtered_images:
            for img_url in filtered_images[:10]:
                send_image(sender_id, img_url, page_token)
                time.sleep(0.3)
            
            msg = f"Mehenna {specific_product} photos dear!\n\nDear 💙"
            send_message(sender_id, msg, page_token)
            save_message(sender_id, ad_id, "assistant", msg)
            
            if not context.get("asked_order"):
                time.sleep(1)
                msg2 = "Order kamathi dha?\n\nDear 💙"
                send_message(sender_id, msg2, page_token)
                save_message(sender_id, ad_id, "assistant", msg2)
                update_user_context(sender_id, asked_order=True)
            return
    
    if product_images:
        for img_url in product_images[:10]:
            send_image(sender_id, img_url, page_token)
            time.sleep(0.3)
        
        msg = "Mehenna photos dear!\n\nDear 💙"
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)
        
        if not context.get("asked_order"):
            time.sleep(1)
            msg2 = "Order kamathi dha?\n\nDear 💙"
            send_message(sender_id, msg2, page_token)
            save_message(sender_id, ad_id, "assistant", msg2)
            update_user_context(sender_id, asked_order=True)
    else:
        msg = "Photos nehe dear, mata message karanna.\n\nDear 💙"
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)


def get_specific_product_images(product_keyword, ad_id):
    """Get images for a specific product only"""
    try:
        records = get_cached_products()
        if not records:
            return []

        for row in records:
            if str(row.get("ad_id")) == str(ad_id) or not ad_id:
                image_urls = []
                
                for i in range(1, 6):
                    name_key = f"product_{i}_name"
                    product_name = str(row.get(name_key, "")).lower()
                    
                    if product_keyword in product_name:
                        for img_num in range(1, 4):
                            image_key = f"product_{i}_image_{img_num}"
                            img_url = row.get(image_key)
                            if img_url and img_url.startswith("http"):
                                image_urls.append(img_url)
                        
                        if image_urls:
                            return image_urls

        return []

    except Exception as e:
        print(f"Error getting specific product images: {e}", flush=True)
        return []


def handle_delivery_request(sender_id, page_token, ad_id, context):
    """Handle delivery charges"""
    msg1 = "Delivery Rs.350 dear! Island-wide.\n\nDear 💙"
    send_message(sender_id, msg1, page_token)
    save_message(sender_id, ad_id, "assistant", msg1)
    
    if not context.get("asked_order"):
        time.sleep(1)
        msg2 = "Order kamathi dha?\n\nDear 💙"
        send_message(sender_id, msg2, page_token)
        save_message(sender_id, ad_id, "assistant", msg2)
        update_user_context(sender_id, asked_order=True)


def handle_details_request(sender_id, user_text, products_context, product_images, page_token, ad_id, context, entities):
    """Handle details - visthara"""
    
    if products_context:
        specific_product = entities.get("product") or extract_product_from_query(user_text)
        
        if not specific_product:
            history = get_cached_conversation_history(sender_id, limit=5)
            
            for msg in reversed(history[-6:]):
                if msg["role"] == "user":
                    product_in_history = extract_product_from_query(msg["message"])
                    if product_in_history:
                        specific_product = product_in_history
                        break
        
        if not specific_product and context.get("product_name"):
            specific_product = context.get("product_name")
        
        if specific_product:
            filtered_details = []
            products_lines = products_context.split("\n")
            
            capturing = False
            for line in products_lines:
                if " - Rs." in line and specific_product in line.lower():
                    capturing = True
                    filtered_details.append(line)
                elif " - Rs." in line and capturing:
                    break
                elif capturing and line.strip():
                    filtered_details.append(line)
            
            if filtered_details:
                details_text = "\n".join(filtered_details)
                msg = f"Mehenna {specific_product} details!\n\n{details_text}\n\nDear 💙"
            else:
                msg = f"Mehenna details!\n\n{products_context}\n\nDear 💙"
        else:
            msg = f"Mehenna details!\n\n{products_context}\n\nDear 💙"
        
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)
        
        if not context.get("asked_order"):
            time.sleep(1)
            msg2 = "Order kamathi dha?\n\nDear 💙"
            send_message(sender_id, msg2, page_token)
            save_message(sender_id, ad_id, "assistant", msg2)
            update_user_context(sender_id, asked_order=True)
    else:
        msg = "Details nehe dear, mata message karanna.\n\nDear 💙"
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)


def handle_how_to_order(sender_id, page_token, ad_id):
    """Handle 'how to order' questions"""
    msg = "Order karanna:\n1. Product select karanna\n2. Location ewanna\n3. Name, address, phone ewanna\n\nMata message karanna dear!\n\nDear 💙"
    send_message(sender_id, msg, page_token)
    save_message(sender_id, ad_id, "assistant", msg)


def get_fallback_response(text, products_context, intent):
    """Fallback response"""
    if products_context:
        return f"Mehenna products:\n\n{products_context}\n\nDear 💙"
    else:
        return "Mata message karanna dear!\n\nDear 💙"


def validate_reply_strict(reply, products_context, user_message):
    """STRICT validation"""
    
    forbidden_words = [
        "fridge", "refrigerator", "samsung", "lg", "microwave",
        "gas cooker", "200l", "frost-free", "5 star", "warranty"
    ]
    
    for word in forbidden_words:
        if word in reply.lower():
            if not products_context or word not in products_context.lower():
                return {"valid": False, "reason": f"Mentioned forbidden product: {word}"}
    
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
        "ඕනා", "කැමති", "kamathi", "hari", "okey", "okay"
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in agreement_keywords)


def handle_contact_details(sender_id, text, page_token, ad_id, products_context):
    """Handle when user sends complete contact details (legacy)"""
    lead_info = extract_full_lead_info(text)
    
    if lead_info.get("phone"):
        save_complete_order(sender_id, ad_id, lead_info, products_context)
        
        confirm_msg = f"Thank you dear! {lead_info.get('phone')} ekata call karanawa soon.\n\nDear 💙"
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

    qty_patterns = [
        r"(?:qty|quantity|keeyek)[:\s]*(\d+)",
        r"(\d+)\s*(?:ekak|ganna|layer|tier)",
    ]
    for pattern in qty_patterns:
        match = re.search(pattern, text.lower())
        if match:
            info["quantity"] = match.group(1)
            break

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if re.match(r"^[A-Z][a-z]+(\s+[A-Z][a-z]+)+$", line):
            info["name"] = line[:50]
            break
        elif i == 0 and not re.search(r'\d{9}', line):
            if re.match(r"^[A-Z][a-z]+", line):
                info["name"] = line[:50]
                break

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

def get_ai_response(user_message, history, products_context, product_images, sender_id, ad_id, context):
    """Generate AI response with context awareness"""
    try:
        system_prompt = """You are a friendly sales assistant for Social Mart Sri Lanka.

LANGUAGE RULES:
1. Use SIMPLE SINGLISH (2-4 words per sentence)
2. Mix Sinhala/English naturally
3. Keep SHORT (1-2 sentences)
4. Always end with "Dear 💙"

PRODUCT RULES:
1. ONLY mention products in "AVAILABLE PRODUCTS"
2. Use EXACT names and prices
3. NEVER invent products

CONTEXT AWARENESS:
- If user asked about a product before, remember it
- Don't repeat questions already asked
- Be natural and conversational

DON'T ask "Order kamathi dha?" in every message!

"""

        if products_context:
            system_prompt += f"\nAVAILABLE PRODUCTS:\n{products_context}\n"
        
        if context.get("product_name"):
            system_prompt += f"\nCONTEXT: User is interested in {context['product_name']}"
        if context.get("location"):
            system_prompt += f"\nCONTEXT: User location is {context['location']}"

        messages = [{"role": "system", "content": system_prompt}]

        for msg in history[-12:]:
            messages.append({"role": msg["role"], "content": msg["message"]})

        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=60,
            temperature=0.3,
            timeout=25
        )

        reply = response.choices[0].message.content.strip()

        if not reply.endswith("Dear 💙") and "Dear 💙" not in reply:
            reply = reply + "\n\nDear 💙"

        return reply

    except (ConnectionError, TimeoutError, httpx.ConnectError, httpx.TimeoutException) as e:
        print(f"OpenAI connection error: {type(e).__name__} - {str(e)}", flush=True)
        return "Sorry dear, issue ekak.\n\nDear 💙"
    except Exception as e:
        print(f"OpenAI error: {e}", flush=True)
        return "Sorry dear, issue ekak.\n\nDear 💙"


# =========================
# Product data from sheets
# =========================

def get_all_products():
    """Get ALL products from all ads"""
    try:
        records = get_cached_products()
        if not records:
            return None, []

        all_products_text = ""
        all_images = []
        seen_products = set()

        for row in records:
            for i in range(1, 6):
                name_key = f"product_{i}_name"
                price_key = f"product_{i}_price"
                details_key = f"product_{i}_details"

                prod_name = row.get(name_key)
                
                if prod_name and prod_name not in seen_products:
                    seen_products.add(prod_name)
                    
                    product_line = f"{prod_name} - {row.get(price_key, '')}"
                    
                    details = row.get(details_key, "")
                    if details:
                        product_line += f"\n{details}"
                    
                    all_products_text += product_line + "\n\n"

                    for img_num in range(1, 4):
                        image_key = f"product_{i}_image_{img_num}"
                        img_url = row.get(image_key)
                        if img_url and img_url.startswith("http") and img_url not in all_images:
                            all_images.append(img_url)

        return all_products_text.strip(), all_images[:20]

    except Exception as e:
        print(f"Error getting all products: {e}", flush=True)
        return None, []


def get_products_for_ad(ad_id):
    """Get products and images for specific ad"""
    try:
        records = get_cached_products()
        if not records:
            return None, []

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
        records = get_cached_products()
        if not records:
            return None, []

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


def get_conversation_history_from_sheet(sender_id, limit=30):
    """Get conversation history FROM SHEET"""
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
