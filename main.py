
# Full version of BeruBot - includes everything (user flow, admin flow, moderation, dashboard)
# Skipping comment headers for brevity

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from flask import Flask, render_template_string, redirect, send_file, request
import threading
import requests
import logging
import base64
import os
import json
import asyncio

request_queue = []
MAX_REQUESTS = 50
EDIT_TRACK_KEYWORD = "#behrupiyaedit"

if os.path.exists("queue.json"):
    with open("queue.json", "r") as f:
        request_queue = json.load(f)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
QUEUE_PASSWORD = os.environ.get("QUEUE_PASSWORD")
UMAMI_URL = os.environ.get("UMAMI_URL")
UMAMI_TOKEN = os.environ.get("UMAMI_TOKEN")
UMAMI_SITE_ID = os.environ.get("UMAMI_SITE_ID")
CREDS_FILE = "credentials.json"

if GOOGLE_CREDENTIALS:
    creds_json = base64.b64decode(GOOGLE_CREDENTIALS).decode('utf-8')
    with open(CREDS_FILE, "w") as f:
        f.write(creds_json)
    with open(CREDS_FILE, "r") as f:
        print("DEBUG CREDENTIALS:", f.read())

def save_queue():
    with open("queue.json", "w") as f:
        json.dump(request_queue, f)

def reset_queue():
    global request_queue
    request_queue.clear()
    if os.path.exists("queue.json"):
        os.remove("queue.json")

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

async def send_temp_message(bot, chat_id, text, **kwargs):
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        await asyncio.sleep(60)
        await bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
    except: pass

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        name = member.full_name
        username = f"@{member.username}" if member.username else name
        await send_temp_message(
            context.bot, update.effective_chat.id,
            f"👋 Welcome {username}!\n\n"
            "📸 This group is for image editing requests only.\n"
            "To request an edit, DM the bot."
        )

async def moderate_group_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private": return
    if update.message.reply_to_message:  return
    
    if update.message.left_chat_member:
       try:
           await update.message.delete()
       except:
           pass
       return
   
    user_id = update.message.from_user.id
    chat = update.message.chat
    member = await context.bot.get_chat_member(chat.id, user_id)
    if member.status in ["administrator", "creator"]: return
    try: await update.message.delete()
    except: pass
    await send_temp_message(
        context.bot, chat.id,
        "⚠️ Only admins can post here. Please DM the bot for any requests."
    )

async def track_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = None
    event_type = None

    if update.message.new_chat_members:
        for member in update.message.new_chat_members:
            user = member
            event_type = "joined"
    elif update.message.left_chat_member:
        user = update.message.left_chat_member
        event_type = "left"

    if user and event_type:
        try:
            data = {
                "type": "event",
                "event_name": f"user_{event_type}",
                "url": "https://btracker-779c.onrender.com",
                "website_id": UMAMI_SITE_ID,
                "timestamp": datetime.utcnow().isoformat(),
                "user_agent": "berubot",
                "data": {
                    "username": user.username or user.full_name,
                    "user_id": user.id
                }
            }
            headers = {"Authorization": f"Bearer {UMAMI_TOKEN}"}
            response  = requests.post(UMAMI_URL, json=data, headers=headers)
            print("UMAMI Response:", response.status_code, response.text)
        except Exception as e:
            print("UMAMI USER TRACK FAILED:", e)

async def track_edit_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != "supergroup": return
    if EDIT_TRACK_KEYWORD in (update.message.caption or ""):
        try:
            data = {
                "type": "event",
                "event_name": "edit_post",
                "url": "https://btracker-779c.onrender.com",
                "website_id": UMAMI_SITE_ID,
                "timestamp": datetime.utcnow().isoformat(),
                "user_agent": "berubot",
                "data": {
                    "caption": update.message.caption,
                    "username": update.message.from_user.username,
                    "photos": len(update.message.photo)
                }
            }
            headers = {"Authorization": f"Bearer {UMAMI_TOKEN}"}
            requests.post(UMAMI_URL, json=data, headers=headers)
        except Exception as e:
            print("UMAMI EDIT TRACK FAILED:", e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    await update.message.reply_text(
        "👋 Welcome! You can submit an image editing request.\n\n"
        "📸 Just send a photo with a caption.\n",
        reply_markup=get_user_menu(uid)
    )

async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != "private": return
    user = update.message.from_user
    if len(request_queue) >= MAX_REQUESTS:
        await update.message.reply_text("Queue full. Try again tomorrow.", reply_markup=get_user_menu(user.id))
        return
    if any(r["id"] == user.id for r in request_queue):
        await update.message.reply_text("You already submitted a request.", reply_markup=get_user_menu(user.id))
        return
    if not update.message.photo:
        await update.message.reply_text("❗ Only image requests allowed. Send a photo + caption.", reply_markup=get_user_menu(user.id))
        return
    if not update.message.caption:
        await update.message.reply_text("📸 Got the image. Next time add a caption too.", reply_markup=get_user_menu(user.id))
    req = {
        "id": user.id, "name": user.username or user.first_name,
        "status": "pending", "type": "photo",
        "photo_id": update.message.photo[-1].file_id,
        "caption": update.message.caption or "No caption",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    request_queue.append(req)
    save_queue()
    await update.message.reply_text(
        f"✅ Request received. You're #{len(request_queue)} in the queue.\n\n"
        "⏱️ SLA: 24–48 hours\n"
        "⚡ Paid fast track available\n"
        "🔐 DM admin for private edits",
        reply_markup=get_user_menu(user.id)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data
    if data == "check_status":
        r = next((r for r in request_queue if r["id"] == uid), None)
        msg = "❌ No request." if not r else ("🕐 Pending..." if r["status"] == "pending" else "✅ Completed!")
        await query.edit_message_text(msg, reply_markup=get_user_menu(uid))
    elif data == "cancel_request":
        i = next((i for i, r in enumerate(request_queue) if r["id"] == uid), None)
        if i is not None:
            request_queue[i]["status"] = "cancelled"
            del request_queue[i]
            save_queue()
            await query.edit_message_text("❌ Cancelled.", reply_markup=get_user_menu(uid))
        else:
            await query.edit_message_text("No request found.", reply_markup=get_user_menu(uid))
    elif data == "submit_request":
        await query.edit_message_text("Send a photo + caption to get started.", reply_markup=get_user_menu(uid))
    elif data.startswith("admin_done:"):
        tid = int(data.split(":")[1])
        for r in request_queue:
            if r["id"] == tid:
                r["status"] = "done"
                save_queue()
                try: await context.bot.send_message(chat_id=tid, text="✅ Your request is completed.")
                except: pass
                await query.edit_message_text(f"{r['name']}'s request marked done.")
                return
        await query.edit_message_text("Request not found.")

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    r = next((r for r in request_queue if r["id"] == uid), None)
    if not r:
        await update.message.reply_text("❌ No request in queue.", reply_markup=get_user_menu(uid))
    elif r["status"] == "pending":
        await update.message.reply_text("🕐 Still pending.", reply_markup=get_user_menu(uid))
    else:
        await update.message.reply_text("✅ Completed!", reply_markup=get_user_menu(uid))

async def show_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("Not authorized.")
        return
    if not request_queue:
        await update.message.reply_text("Queue is empty.")
        return
    for i, r in enumerate(request_queue, 1):
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("Mark as Done", callback_data=f"admin_done:{r['id']}")]])
        await update.message.reply_text(f"{i}. {r['name']} - {r['type']} - {r['status']}", reply_markup=btn)

async def manual_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.message.from_user.id):
        reset_queue()
        await update.message.reply_text("Queue reset.")
    else:
        await update.message.reply_text("Not authorized.")

flask_app = Flask(__name__)
TEMPLATE = """<!doctype html><title>Queue</title><h2>Queue ({{ queue|length }}/{{ max_requests }})</h2><ul>
{% for r in queue %}
<li><b>{{ r.name }}</b> - {{ r.type }} - <i>{{ r.status }}</i><br>
{% if r.type == 'photo' %}
<a href="https://api.telegram.org/file/bot{{ bot_token }}/{{ r.file_path }}" target="_blank">Download</a><br>
<i>{{ r.caption }}</i>
{% endif %}</li><hr>
{% endfor %}</ul>
"""


USER_TEMPLATE = """
<!doctype html>
<title>Queue Status</title>
<h2>Current Queue ({{ queue|length }})</h2>
<table border="1" cellspacing="0" cellpadding="5">
    <tr>
        <th>#</th>
        <th>User</th>
        <th>Status</th>
        <th>Expected Delivery</th>
    </tr>
    {% for r in queue %}
    <tr>
        <td>{{ loop.index }}</td>
        <td>{{ r.name }}</td>
        <td>{{ r.status }}</td>
        <td>{{ r.expected }}</td>
    </tr>
    {% endfor %}
</table>
"""

LANDING_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>BeruBot – Behrupiya Edits</title>
  <style>
    body {
      background-color: #000;
      color: #fff;
      font-family: 'Courier New', Courier, monospace;
      padding: 40px;
      text-align: center;
    }
    h1 {
      font-size: 2.5em;
      margin-bottom: 10px;
    }
    p {
      font-size: 1.1em;
      margin: 10px auto;
      max-width: 600px;
    }
    ul {
      list-style: none;
      padding: 0;
      margin: 20px 0;
    }
    ul li {
      background: #111;
      margin: 10px auto;
      padding: 10px 20px;
      border: 1px solid #333;
      max-width: 400px;
      border-radius: 6px;
    }
    a {
      color: #fff;
      background: #333;
      padding: 10px 20px;
      text-decoration: none;
      border-radius: 6px;
      display: inline-block;
      margin: 10px;
      transition: background 0.3s ease;
    }
    a:hover {
      background: #fff;
      color: #000;
    }
    hr {
      margin: 30px auto;
      width: 60%;
      border: 1px solid #444;
    }
  </style>
</head>
<body>
  <h1>Welcome to Behrupiya Edits 💀</h1>
  <p>BeruBot is your NSFW fantasy image editing genie.</p>
  <ul>
    <li>💥 Realistic edits of your wildest dreams</li>
    <li>🔞 NSFW with a purpose — Respect in Real Life</li>
    <li>⏱️ Free queue: 24–48hr SLA</li>
    <li>⚡ Fast delivery: Paid options available</li>
  </ul>
  <a href="https://t.me/behrupiya_bot" target="_blank">👉 Chat with BeruBot on Telegram</a>
  <hr>
  <a href="/status">View Public Queue</a>
</body>
</html>
"""


@flask_app.route("/")
def landing_page():
    return render_template_string(LANDING_TEMPLATE)


# @flask_app.route("/adminbeh")
# def admin_queue():
#     display = []
#     for r in request_queue:
#         item = r.copy()
#         if r["type"] == "photo":
#             try:
#                 f = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={r['photo_id']}").json()
#                 item["file_path"] = f["result"]["file_path"]
#             except:
#                 item["file_path"] = ""
#         display.append(item)
#     return render_template_string(TEMPLATE, queue=display, bot_token=BOT_TOKEN, max_requests=MAX_REQUESTS)


@flask_app.route("/adminbeh")
def admin_queue():
    pwd = request.args.get("password")
    if pwd != QUEUE_PASSWORD:
        return "Unauthorized. Invalid password.", 401

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



# @flask_app.route("/")
# def index():
#     display = []
#     for r in request_queue:
#         item = r.copy()
#         if r["type"] == "photo":
#             try:
#                 f = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={r['photo_id']}").json()
#                 item["file_path"] = f["result"]["file_path"]
#             except: item["file_path"] = ""
#         display.append(item)
#     return render_template_string(TEMPLATE, queue=display, bot_token=BOT_TOKEN, max_requests=MAX_REQUESTS)

@flask_app.route("/reset", methods=["GET", "POST"])
def reset(): reset_queue(); return redirect("/")

@flask_app.route("/download-queue")
def download_queue():
    pwd = request.args.get("password")
    if pwd != QUEUE_PASSWORD:
        return "Unauthorized. Invalid password.", 401

    if os.path.exists("queue.json"):
        return send_file("queue.json", as_attachment=True)
    return "No queue file found.", 404


@flask_app.route("/restore-queue", methods=["POST"])
def restore_queue():
    pwd = request.args.get("password")
    if pwd != QUEUE_PASSWORD:
        return "Unauthorized", 401

    data = request.get_json()
    if not isinstance(data, list):
        return "Invalid format", 400

    with open("queue.json", "w") as f:
        json.dump(data, f)

    return "Queue restored", 200


@flask_app.route("/status")
def public_status():
    display = []
    for r in request_queue:
        item = {
            "name": r["name"],
            "status": r["status"]
        }
        try:
            # Extract timestamp from each request (add it when appending to queue)
            timestamp = r.get("timestamp")
            if timestamp:
                dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                item["expected"] = (dt + timedelta(hours=48)).strftime("%b %d, %I:%M %p")
            else:
                item["expected"] = "Unknown"
        except:
            item["expected"] = "Unknown"
        display.append(item)
    return render_template_string(USER_TEMPLATE, queue=display)



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
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.NEW_CHAT_MEMBERS), moderate_group_messages), group=True)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER, track_membership), group=True)    
    app.add_handler(MessageHandler(filters.ALL & filters.Caption(EDIT_TRACK_KEYWORD), track_edit_posts), group=True)


    app.run_polling()
