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
import asyncio

request_queue = []
MAX_REQUESTS = 50

if os.path.exists("queue.json"):
    with open("queue.json", "r") as f:
        request_queue = json.load(f)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
CREDS_FILE = "credentials.json"

if GOOGLE_CREDENTIALS:
    creds_json = base64.b64decode(GOOGLE_CREDENTIALS).decode('utf-8')
    with open(CREDS_FILE, "w") as f:
        f.write(creds_json)
    with open(CREDS_FILE, "r") as f:
        print("🔍 DEBUG: Contents of credentials.json:\n", f.read())

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

def save_queue():
    with open("queue.json", "w") as f:
        json.dump(request_queue, f)

def reset_queue():
    global request_queue
    request_queue.clear()
    if os.path.exists("queue.json"):
        os.remove("queue.json")
    print("Queue and file cleared.")

async def send_temp_message(bot, chat_id, text, **kwargs):
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        await asyncio.sleep(60)
        await bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
    except Exception as e:
        print("⚠️ Failed to auto-delete message:", e)

# Welcome new users with temp message
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        name = member.full_name
        username = f"@{member.username}" if member.username else name
        await send_temp_message(
            context.bot,
            update.effective_chat.id,
            f"👋 Welcome {username}!\n\n📸 This group is for image editing requests only.\nTo request an image edit, please DM me directly."
        )

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
        await send_temp_message(
            context.bot,
            chat.id,
            "⚠️ Only admins can post here. All image editing requests must be sent to the bot via DM."
        )

# === Add rest of bot logic ===
# ... (start, handle_request, check_status, handle_callback, reset, show_queue, etc.)

# Flask web dashboard
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
