# bot_linking.py
import os, requests
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

API_BASE = os.getenv("API_BASE", "http://localhost:8080")
API_KEY  = os.getenv("API_KEY", "")  # ίδιο με το server

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Στείλε: /link 123456 (το PIN από την εφαρμογή)")
        return
    pin = context.args[0].strip()
    tg = str(update.effective_user.id)
    try:
        r = requests.post(f"{API_BASE}/api/link/confirm",
                          headers={"X-API-Key": API_KEY},
                          json={"pin": pin, "tg": tg}, timeout=10)
        if r.status_code < 300:
            await update.message.reply_text("✅ Συνδέθηκε επιτυχώς η εφαρμογή με το λογαριασμό σου.")
        else:
            await update.message.reply_text(f"❌ Αποτυχία: {r.text}")
    except Exception as e:
        await update.message.reply_text(f"❌ Σφάλμα δικτύου: {e}")

def add_link_handlers(application):
    application.add_handler(CommandHandler("link", link_cmd))
