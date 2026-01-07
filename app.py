import os
import requests
import json
import re
from flask import Flask, request
from openai import OpenAI
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

# Initialize OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# User conversation state tracking
user_states = {}

# =====================
# CACHING SYSTEM - NEW!
# =====================

# Cache for product data
products_cache = {
    "data": None,
    "timestamp": 0,
    "ttl": 300  # 5 minutes
}

# Cache for conversation history per user
conversation_cache = {}
CONVERSATION_CACHE_TTL = 60  # 1 minute


def get_cached_products():
    """Get products from cache or fetch if expired"""
    current_time = time.time()
    
    if products_cache["data"] and (current_time - products_cache["timestamp"]) < products_cache["ttl"]:
        print("✅ Using cached products", flush=True)
        return products_cache["data"]
    
    # Fetch fresh data
    print("📥 Fetching fresh products from sheet", flush=True)
    sheet = get_sheet()
    if not sheet:
        return None
    
    try:
        ad_products_sheet = sheet.worksheet("Ad_Products")
        records = ad_products_sheet.get_all_records()
        
        # Update cache
        products_cache["data"] = records
        products_cache["timestamp"] = current_time
        
        print(f"✅ Cached {len(records)} product rows", flush=True)
        return records
    except Exception as e:
        print(f"Error fetching products: {e}", flush=True)
        return products_cache["data"]  # Return stale cache if error


def get_cached_conversation_history(sender_id, limit=30):
    """Get conversation history with caching"""
    current_time = time.time()
    cache_key = f"{sender_id}_{limit}"
    
    if cache_key in conversation_cache:
        cached_data, cached_time = conversation_cache[cache_key]
        if (current_time - cached_time) < CONVERSATION_CACHE_TTL:
            print(f"✅ Using cached history for {sender_id}", flush=True)
            return cached_data
    
    # Fetch fresh data
    print(f"📥 Fetching fresh history for {sender_id}", flush=True)
    history = get_conversation_history_from_sheet(sender_id, limit)
    
    # Update cache
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
            "last_topic": None,
            "asked_location": False,
            "asked_order": False,
            "order_retry_count": 0,
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

                    if "referral" in event:
                        ad_id = event["referral"].get("ref")
                        handle_ad_referral(sender_id, ad_id, page_token)

                    if event.get("message") and "text" in event["message"]:
                        text = event["message"]["text"]
                        print(f"Message from {sender_id}: {text}", flush=True)
                        
                        # Clear cache for this user
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
    """Main message handler with FULL CONTEXT AWARENESS"""
    try:
        context = get_user_context(sender_id)
        ad_id = context.get("ad_id") or get_user_ad_id(sender_id)
        
        save_message(sender_id, ad_id, "user", text)

        products_context, product_images = get_products_for_ad(ad_id) if ad_id else (None, [])
        
        if not products_context:
            products_context, product_images = search_products_by_query(text)
            print(f"Searched products, found: {bool(products_context)}", flush=True)
        
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
        
        if detect_contact_details(text):
            handle_contact_details(sender_id, text, page_token, ad_id, products_context)
            return

        intent = detect_intent(text, history)
        print(f"🎯 Intent: {intent}, Step: {context.get('step')}", flush=True)

        if intent == "product_availability":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_availability_request(sender_id, text, products_context, product_images, page_token, ad_id, context)
            return
        elif intent == "photos":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_photo_request(sender_id, text, products_context, product_images, page_token, ad_id, context)
            return
        elif intent == "delivery":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_delivery_request(sender_id, page_token, ad_id, context)
            return
        elif intent == "details":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_details_request(sender_id, text, products_context, product_images, page_token, ad_id, context)
            return
        elif intent == "height":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_height_request(sender_id, text, products_context, page_token, ad_id, context)
            return
        elif intent == "how_to_order":
            update_user_context(sender_id, step=None, order_retry_count=0)
            handle_how_to_order(sender_id, page_token, ad_id)
            return

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
            
            if "thiyanawada" in text.lower() or "thiyanawadha" in text.lower():
                update_user_context(sender_id, step=None, order_retry_count=0)
                handle_availability_request(sender_id, text, products_context, product_images, page_token, ad_id, context)
                return
            
            if wants_order:
                update_user_context(sender_id, step="collect_details", order_retry_count=0)
                details_msg = "Super! Name, address, phone ewanna.\n\nDear 💙"
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
                retry_msg = "Prashna thiyanawada dear? Mata kiyanna.\n\nDear 💙"
                send_message(sender_id, retry_msg, page_token)
                save_message(sender_id, ad_id, "assistant", retry_msg)
                return
        
        elif step == "collect_details":
            lead_info = extract_full_lead_info(text)
            if lead_info.get("phone"):
                handle_contact_details(sender_id, text, page_token, ad_id, products_context)
                return

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
# Intent detection
# ======================

def detect_intent(text, history):
    """Detect user intent from message"""
    text_lower = text.lower()
    
    availability_keywords = ["thiyanawada", "thiyanawadha", "ithiri thiyanawada", "stock thiyanawada", "available da"]
    if any(kw in text_lower for kw in availability_keywords):
        return "product_availability"
    
    photo_keywords = ["photo", "photos", "foto", "fotos", "pic", "pics", "picture", "pictures", 
                     "image", "images", "mata photos", "photos dana", "photos ewanna",
                     "pics dana", "photo ekak", "image ekak", "foto ekak", "picture ekak",
                     "පින්තූර", "පින්තූරය", "dana puluwang", "puluwang dha photo", "ewanna photo"]
    if any(kw in text_lower for kw in photo_keywords):
        return "photos"
    
    delivery_keywords = ["delivery", "delivery charge", "delivery charges", "chargers", 
                        "delivery fee", "delivery kiyada", "delivery cost", "delivery eka kiyada",
                        "delivery charges kiyada", "delivery ekkada", "charges kiyada"]
    if any(kw in text_lower for kw in delivery_keywords):
        return "delivery"
    
    details_keywords = ["details", "visthara", "visthara denna", "thawa visthara", 
                       "mata visthara", "more info", "info", "specification", "tika onai",
                       "onai dha", "details tika", "thawa details", "visthara dena"]
    if any(kw in text_lower for kw in details_keywords):
        return "details"
    
    height_keywords = ["height", "uchayak", "height eka", "uyathai", "size", 
                      "dimensions", "kiyada height"]
    if any(kw in text_lower for kw in height_keywords):
        return "height"
    
    order_keywords = ["kohamada order", "kohomada order", "order karanne kohomada",
                     "how to order", "order karanna", "order karanawada"]
    if any(kw in text_lower for kw in order_keywords):
        return "how_to_order"
    
    products_keywords = ["mona products", "products mona"]
    if any(kw in text_lower for kw in products_keywords):
        return "product_list"
    
    return "general"


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
# Context-aware handlers
# ======================

def handle_availability_request(sender_id, user_text, products_context, product_images, page_token, ad_id, context):
    """Handle 'thiyanawada' questions"""
    
    specific_product = extract_product_from_query(user_text)
    
    if not specific_product:
        specific_product = context.get("product_name")
    
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


def handle_photo_request(sender_id, user_text, products_context, product_images, page_token, ad_id, context):
    """Handle photos with context awareness"""
    
    specific_product = extract_product_from_query(user_text)
    
    if not specific_product and context.get("product_name"):
        specific_product = context.get("product_name")
        print(f"📸 Using product from context: {specific_product}", flush=True)
    
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
    """Get images for a specific product only - USES CACHE"""
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
    """Handle delivery charges - context aware"""
    msg1 = "Delivery Rs.350 dear! Island-wide.\n\nDear 💙"
    send_message(sender_id, msg1, page_token)
    save_message(sender_id, ad_id, "assistant", msg1)
    
    if not context.get("asked_order"):
        time.sleep(1)
        msg2 = "Order kamathi dha?\n\nDear 💙"
        send_message(sender_id, msg2, page_token)
        save_message(sender_id, ad_id, "assistant", msg2)
        update_user_context(sender_id, asked_order=True)


def handle_details_request(sender_id, user_text, products_context, product_images, page_token, ad_id, context):
    """Handle details with FULL CONTEXT AWARENESS"""
    
    if products_context:
        specific_product = extract_product_from_query(user_text)
        
        if not specific_product:
            history = get_cached_conversation_history(sender_id, limit=5)
            
            for msg in reversed(history[-6:]):
                if msg["role"] == "user":
                    product_in_history = extract_product_from_query(msg["message"])
                    if product_in_history:
                        specific_product = product_in_history
                        print(f"📝 Found product in history: {specific_product}", flush=True)
                        break
        
        if not specific_product and context.get("product_name"):
            specific_product = context.get("product_name")
            print(f"📝 Using product from context: {specific_product}", flush=True)
        
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


def handle_height_request(sender_id, user_text, products_context, page_token, ad_id, context):
    """Handle height with context awareness"""
    
    specific_product = extract_product_from_query(user_text) or context.get("product_name")
    
    if products_context:
        if "height" in products_context.lower() or "cm" in products_context.lower():
            msg = f"Height details:\n\n{products_context}\n\nDear 💙"
        else:
            msg = f"Product details:\n\n{products_context}\n\nDear 💙"
        
        send_message(sender_id, msg, page_token)
        save_message(sender_id, ad_id, "assistant", msg)
        
        if not context.get("asked_order"):
            time.sleep(1)
            msg2 = "Order kamathi dha?\n\nDear 💙"
            send_message(sender_id, msg2, page_token)
            save_message(sender_id, ad_id, "assistant", msg2)
            update_user_context(sender_id, asked_order=True)
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
    """Fallback response"""
    if intent == "product_list" or "mona" in text.lower():
        if products_context:
            return f"Mehenna products:\n\n{products_context}\n\nDear 💙 SEND_IMAGES START_LOCATION_FLOW"
        else:
            return "Products nehe dear, mata message karanna.\n\nDear 💙"
    
    if "thiyanawadha" in text.lower() or "thiyanawada" in text.lower():
        if products_context:
            return f"Ow thiyanawa dear!\n\n{products_context}\n\nDear 💙 SEND_IMAGES START_LOCATION_FLOW"
        else:
            return "Nehe dear, eka nehe.\n\nDear 💙"
    
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
                        return {"valid": False, "reason": f"Hallucinated: {match}"}
            else:
                return {"valid": False, "reason": "Mentioned products when none available"}
    
    if "thiyanawadha" in user_message.lower() or "thiyanawada" in user_message.lower():
        if "ow" not in reply.lower() and "nehe" not in reply.lower():
            return {"valid": False, "reason": "Didn't answer yes/no question"}
    
    if "price on request" in reply.lower():
        return {"valid": False, "reason": "Hallucinated 'price on request'"}
    
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
        )

        reply = response.choices[0].message.content.strip()

        if not reply.endswith("Dear 💙") and "Dear 💙" not in reply:
            reply = reply + "\n\nDear 💙"

        return reply

    except Exception as e:
        print(f"OpenAI error: {e}", flush=True)
        return "Sorry dear, issue ekak.\n\nDear 💙"


# =========================
# Product data from sheets - WITH CACHING
# =========================

def get_products_for_ad(ad_id):
    """Get products and images for specific ad - USES CACHE"""
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
    """Search ALL products in sheet by query - USES CACHE"""
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
    """Get conversation history FROM SHEET (not cached)"""
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
