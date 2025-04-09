from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          filters, ContextTypes, CallbackQueryHandler)
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from flask import Flask, render_template_string, redirect
import threading
import requests
import logging
import base64
import os
import json

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
CREDS_FILE = "credentials.json"
LAST_ADMIN_FILE = "last_admin.json"

if GOOGLE_CREDENTIALS:
    creds_json = base64.b64decode(GOOGLE_CREDENTIALS).decode('utf-8')
    with open(CREDS_FILE, "w") as f:
        f.write(creds_json)
    with open(CREDS_FILE, "r") as f:
        print("🔍 DEBUG: Contents of credentials.json:\n", f.read())

SPREADSHEET_NAME = "Request_Sheet"
MAX_REQUESTS = 50
request_queue = []

if os.path.exists("queue.json"):
    with open("queue.json", "r") as f:
        request_queue = json.load(f)

def save_queue():
    with open("queue.json", "w") as f:
        json.dump(request_queue, f)

def save_last_admin_id(message_id):
    with open(LAST_ADMIN_FILE, "w") as f:
        json.dump({"last_admin_message_id": message_id}, f)

def load_last_admin_id():
    if os.path.exists(LAST_ADMIN_FILE):
        with open(LAST_ADMIN_FILE, "r") as f:
            return json.load(f).get("last_admin_message_id")
    return None

def is_admin(user_id):
    return user_id == ADMIN_ID

def get_user_menu(user_id):
    has_request = any(r["id"] == user_id for r in request_queue)
    buttons = [[InlineKeyboardButton("Check Status", callback_data="check_status")]]
    if has_request:
        buttons.append([InlineKeyboardButton("Cancel Request", callback_data="cancel_request")])
    else:
        buttons.append([InlineKeyboardButton("Submit Request", callback_data="submit_request")])
    return InlineKeyboardMarkup(buttons)

def reset_queue():
    global request_queue
    request_queue.clear()
    if os.path.exists("queue.json"):
        os.remove("queue.json")
    print("Queue and file cleared.")

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    SHEETS_ENABLED = True
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)
    client = gspread.authorize(creds)
    sheet = client.open(SPREADSHEET_NAME).sheet1
except Exception as e:
    SHEETS_ENABLED = False
    logging.warning("Google Sheets setup failed: %s", e)

def log_to_sheet(request):
    if not SHEETS_ENABLED:
        return
    try:
        values = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), request["name"],
            request["type"], request.get("caption", request.get("content", "")), request["status"]
        ]
        sheet.append_row(values)
    except Exception as e:
        logging.warning("Log error: %s", e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text(
        "👋 Welcome! You can submit an image editing request.\n\n"
        "📸 Just send a photo with a caption describing what you want.",
        reply_markup=get_user_menu(user_id))

async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != "private":
        return
    user = update.message.from_user
    if len(request_queue) >= MAX_REQUESTS:
        await update.message.reply_text("Queue full. Try again tomorrow!", reply_markup=get_user_menu(user.id))
        return
    if any(r["id"] == user.id for r in request_queue):
        await update.message.reply_text("You already submitted a request.", reply_markup=get_user_menu(user.id))
        return
    if not update.message.photo:
        await update.message.reply_text(
            "❗ Only image-based requests are supported.\n\n"
            "Please send a photo with a caption describing what you want edited.",
            reply_markup=get_user_menu(user.id))
        return
    if not update.message.caption:
        await update.message.reply_text(
            "📸 Got your image! \n\n"
            "📝 But please add a caption next time to help us understand your request.",
            reply_markup=get_user_menu(user.id))
    req = {
        "id": user.id,
        "name": user.username or user.first_name,
        "status": "pending",
        "type": "photo",
        "photo_id": update.message.photo[-1].file_id,
        "caption": update.message.caption or "No caption"
    }
    request_queue.append(req)
    save_queue()
    log_to_sheet(req)
    await update.message.reply_text(
        f"✅ Request received! You're #{len(request_queue)} in the queue.\n\n"
        "⏱️ SLA: Your request will be fulfilled within 24–48 hours.\n"
        "⚡ For priority delivery, paid options are available. Ping Admin @Recs55 \n"
        "📤 All images will be publicly shared unless you DM the admin for private edits.",
        reply_markup=get_user_menu(user.id))

# 👇 New admin post tracker
async def track_admin_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    member = await context.bot.get_chat_member(chat.id, user.id)
    if member.status in ["administrator", "creator"]:
        context.chat_data["last_admin_message_id"] = update.message.message_id
        save_last_admin_id(update.message.message_id)

# Updated welcome with admin message forward
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        name = member.full_name
        username = f"@{member.username}" if member.username else name
        await update.message.reply_text(
            f"👋 Welcome {username}!\n\n"
            "📸 This group is for image editing requests only."
            "To request an image edit, please DM me directly.")

        now = datetime.utcnow()
        last_shared = context.chat_data.get("last_reshare_time")

        if "last_admin_message_id" not in context.chat_data:
            context.chat_data["last_admin_message_id"] = load_last_admin_id()

        if not last_shared or (now - last_shared) > timedelta(seconds=60):
            msg_id = context.chat_data.get("last_admin_message_id")
            if msg_id:
                try:
                    await context.bot.forward_message(
                        chat_id=update.effective_chat.id,
                        from_chat_id=update.effective_chat.id,
                        message_id=msg_id
                    )
                    context.chat_data["last_reshare_time"] = now
                except Exception as e:
                    print("⚠️ Failed to forward admin message:", e)

# 2. Delete any non-admin messages in the group and warn them
async def moderate_group_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        return

    user_id = update.message.from_user.id
    chat = update.message.chat

    if update.message.reply_to_message:
        return

    if update.message.left_chat_member:
        return

    member = await context.bot.get_chat_member(chat.id, user_id)
    if member.status not in ["administrator", "creator"]:
        try:
            await update.message.delete()
        except:
            pass
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚠️ Only admins can post here. All image editing requests must be sent to the bot via DM."
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "check_status":
        r = next((r for r in request_queue if r["id"] == user_id), None)
        msg = "❌ No request found." if not r else (
            "🕐 Pending..." if r["status"] == "pending" else "✅ Completed!")
        await query.edit_message_text(msg, reply_markup=get_user_menu(user_id))

    elif data == "cancel_request":
        i = next((i for i, r in enumerate(request_queue) if r["id"] == user_id), None)
        if i is not None:
            cancelled_request = request_queue.pop(i)
            cancelled_request["status"] = "cancelled"
            log_to_sheet(cancelled_request)
            save_queue()
            await query.edit_message_text("❌ Your request has been canceled.", reply_markup=get_user_menu(user_id))
        else:
            await query.edit_message_text("No active request.", reply_markup=get_user_menu(user_id))

    elif data == "submit_request":
        await query.edit_message_text(
            "📸 To submit a request:\n\nPlease send a photo with a caption describing what you want edited.",
            reply_markup=get_user_menu(user_id)
        )

    elif data.startswith("admin_done:"):
        if not is_admin(user_id):
            await query.edit_message_text("Not authorized.", reply_markup=get_user_menu(user_id))
            return
        tid = int(data.split(":")[1])
        for r in request_queue:
            if r["id"] == tid:
                r["status"] = "done"
                save_queue()
                await query.edit_message_text(
                    f"{r['name']}'s request marked done.",
                    reply_markup=get_user_menu(user_id)
                )
                try:
                    await context.bot.send_message(chat_id=tid, text="✅ Your request has been completed!")
                except:
                    pass
                return
        await query.edit_message_text("User not found.", reply_markup=get_user_menu(user_id))

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    request = next((r for r in request_queue if r["id"] == user_id), None)
    if not request:
        await update.message.reply_text("❗ You have no active request in the queue.", reply_markup=get_user_menu(user_id))
    elif request["status"] == "pending":
        await update.message.reply_text("🕐 Your request is still pending.", reply_markup=get_user_menu(user_id))
    else:
        await update.message.reply_text("✅ Your request has been completed!", reply_markup=get_user_menu(user_id))

async def show_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("Not authorized.")
        return
    if not request_queue:
        await update.message.reply_text("Queue is empty.")
        return
    for i, r in enumerate(request_queue, 1):
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("Mark as Done", callback_data=f"admin_done:{r['id']}")]
        ])
        text = f"{i}. {r['name']} - {r['type']} - {r['status']}"
        await update.message.reply_text(text, reply_markup=btn)

async def manual_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.message.from_user.id):
        reset_queue()
        await update.message.reply_text("Queue reset.")
    else:
        await update.message.reply_text("Not authorized.")

# Flask app for web queue view
flask_app = Flask(__name__)
TEMPLATE = """
<!doctype html>
<title>Queue</title>
<h2>Current Queue ({{ queue|length }}/{{ max_requests }})</h2>
<ul>
{% for r in queue %}
  <li><b>{{ r.name }}</b> - {{ r.type }} - <i>{{ r.status }}</i><br>
  {% if r.type == 'photo' %}
    <a href="https://api.telegram.org/file/bot{{ bot_token }}/{{ r.file_path }}" target="_blank">Download</a><br>
    <i>{{ r.caption }}</i>
  {% endif %}
  </li><hr>
{% endfor %}
</ul>
<form action="/reset" method="post"><button>Reset Queue</button></form>
"""

@flask_app.route("/")
def index():
    display = []
    for r in request_queue:
        item = r.copy()
        if r["type"] == "photo":
            try:
                f = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={r['photo_id']}").json()
                item["file_path"] = f["result"]["file_path"]
            except:
                item["file_path"] = ""
        display.append(item)
    return render_template_string(TEMPLATE, queue=display, bot_token=BOT_TOKEN, max_requests=MAX_REQUESTS)

@flask_app.route("/reset", methods=["POST"])
def reset():
    reset_queue()
    return redirect("/")

if __name__ == "__main__":
    threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=8080)).start()
    scheduler = BackgroundScheduler()
    scheduler.add_job(reset_queue, 'cron', hour=0, minute=0)
    scheduler.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", check_status))
    app.add_handler(CommandHandler("queue", show_queue))
    app.add_handler(CommandHandler("reset", manual_reset))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_request))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, track_admin_post), group=True)
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.NEW_CHAT_MEMBERS), moderate_group_messages), group=True)

    app.run_polling()
