#!/usr/bin/env python3
"""
ربات تلگرام «باشگینو» — بدون نیاز به نصب کتابخانه (فقط پایتون ۳.۸+)

کارها:
  • دکمه «باز کردن پنل» (مینی‌اپ) را در منوی ربات و پیام خوش‌آمد قرار می‌دهد
  • وقتی ورزشکار با لینک دعوت مربی وارد ربات شود، به مربی خبر می‌دهد

اجرا:
  export BOT_TOKEN="123456:ABC..."
  export WEBAPP_URL="https://your-site.netlify.app/"
  python3 bot.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()
APP_NAME = os.environ.get("APP_NAME", "باشگینو")

if not TOKEN or not WEBAPP_URL:
    sys.exit("BOT_TOKEN و WEBAPP_URL را تنظیم کنید (راهنما در README).")
if not WEBAPP_URL.startswith("https://"):
    sys.exit("آدرس WEBAPP_URL باید با https:// شروع شود.")

API = f"https://api.telegram.org/bot{TOKEN}/"


def call(method, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(API + method, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=75) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        print(f"[{method}] HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}")
    except Exception as e:  # network hiccup
        print(f"[{method}] {e}")
    return None


def webapp_url(bot_username):
    sep = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{sep}bot={urllib.parse.quote(bot_username)}"


def open_button(url, text="🏋️ باز کردن پنل مربی"):
    return {"inline_keyboard": [[{"text": text, "web_app": {"url": url}}]]}


def display_name(user):
    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x)
    if user.get("username"):
        name += f" (@{user['username']})"
    return name or str(user.get("id"))


def handle_start(msg, payload, url):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})

    if payload.startswith("join_"):
        coach_id = payload[5:]
        if coach_id.isdigit() and int(coach_id) != user.get("id"):
            call("sendMessage", chat_id=chat_id,
                 text=f"سلام {user.get('first_name', '')} 👋\n"
                      f"شما با لینک دعوت مربی‌تان وارد {APP_NAME} شدید.\n"
                      "به مربی خبر دادیم؛ به‌زودی برنامه تمرینی و غذایی‌تان را دریافت می‌کنید. 💪")
            phone_hint = "" if user.get("username") else "\n(این کاربر یوزرنیم ندارد؛ برای ارتباط از شماره‌اش استفاده کنید.)"
            call("sendMessage", chat_id=int(coach_id),
                 text=f"🔔 ورزشکار جدید از طریق لینک دعوت شما وارد شد:\n👤 {display_name(user)}{phone_hint}\n\n"
                      "از بخش «ورزشکاران» او را به لیست اضافه کنید.",
                 reply_markup=open_button(url, "➕ افزودن در پنل"))
            return

    call("sendMessage", chat_id=chat_id,
         text=f"سلام {user.get('first_name', '')} 👋\nبه {APP_NAME} خوش آمدید — سامانه مدیریت ورزشکاران، "
              "برنامه تمرینی و رژیم غذایی برای مربیان شخصی و باشگاه‌ها.\n\nبرای شروع روی دکمه زیر بزنید 👇",
         reply_markup=open_button(url))


def main():
    me = call("getMe")
    if not me:
        sys.exit("توکن ربات درست نیست یا اینترنت در دسترس نیست.")
    url = webapp_url(me["username"])
    print(f"ربات @{me['username']} روشن شد. پنل: {url}")

    call("setChatMenuButton", menu_button={"type": "web_app", "text": "پنل مربی", "web_app": {"url": url}})
    call("setMyCommands", commands=[
        {"command": "start", "description": "شروع و باز کردن پنل"},
        {"command": "panel", "description": "باز کردن پنل مربی"},
        {"command": "invite", "description": "دریافت لینک دعوت ورزشکار"},
        {"command": "help", "description": "راهنما"},
    ])

    offset = 0
    while True:
        updates = call("getUpdates", offset=offset, timeout=60, allowed_updates=["message"])
        if updates is None:
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if not msg or msg.get("chat", {}).get("type") != "private":
                continue
            text = (msg.get("text") or "").strip()
            chat_id = msg["chat"]["id"]
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0]
            if cmd == "/start":
                handle_start(msg, arg.strip(), url)
            elif cmd == "/panel":
                call("sendMessage", chat_id=chat_id, text="پنل مربی 👇", reply_markup=open_button(url))
            elif cmd == "/invite":
                link = f"https://t.me/{me['username']}?start=join_{msg['from']['id']}"
                call("sendMessage", chat_id=chat_id,
                     text=f"لینک دعوت اختصاصی شما 👇\n{link}\n\nاین لینک را برای ورزشکارانتان بفرستید.")
            elif cmd == "/help":
                call("sendMessage", chat_id=chat_id,
                     text="📌 راهنما\n/panel — باز کردن پنل مربی\n/invite — لینک دعوت ورزشکار\n\n"
                          "همه کارها (ورزشکاران، برنامه تمرینی، رژیم، اشتراک) داخل پنل انجام می‌شود.")
            elif text:
                call("sendMessage", chat_id=chat_id, text="برای مدیریت ورزشکاران پنل را باز کنید 👇",
                     reply_markup=open_button(url))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("خاموش شد.")
