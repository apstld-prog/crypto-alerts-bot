
import os
import time
import threading
from datetime import datetime

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from sqlalchemy import select, text

from db import init_db, session_scope, User, Subscription, Alert
from worker_logic import run_alert_cycle

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_URL = os.getenv("WEB_URL")
ADMIN_KEY = os.getenv("ADMIN_KEY")
INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

HELP_TEXT = (
    "🤖 *Crypto Alerts Bot*\n"
    "/start - register\n"
    "/stats - show bot stats\n"
    "/cancel_autorenew - stop future billing, keep access until period end\n"
    "/help - show this help\n"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id) if update.effective_user else None
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
            session.add(user)
            session.flush()
    await update.message.reply_text("👋 Welcome! You're registered.\nUse /help for commands.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with session_scope() as session:
        users = session.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
        premium = session.execute(text("SELECT COUNT(*) FROM users WHERE is_premium = 1")).scalar_one()
        alerts = session.execute(text("""
            SELECT COUNT(*) FROM alerts
            WHERE enabled = 1 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
        """)).scalar_one()
        subs = session.execute(text("""
            SELECT COUNT(*) FROM subscriptions WHERE status_internal IN ('ACTIVE','CANCEL_AT_PERIOD_END')
        """)).scalar_one()
    msg = (
        "📊 *Bot Stats*\n\n"
        f"👥 Users: {users}\n"
        f"💎 Premium users: {premium}\n"
        f"🔔 Active alerts: {alerts}\n"
        f"🧾 Subscriptions: ACTIVE_OR_CANCEL_AT_PERIOD_END={subs}\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_cancel_autorenew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEB_URL or not ADMIN_KEY:
        await update.message.reply_text("⚠️ Cancel not available right now. Try again later.")
        return
    tg_id = str(update.effective_user.id) if update.effective_user else None
    try:
        r = requests.post(
            f"{WEB_URL}/billing/paypal/cancel",
            params={"telegram_id": tg_id, "key": ADMIN_KEY},
            timeout=20,
        )
        if r.status_code == 200:
            data = r.json()
            until = data.get("keeps_access_until")
            if until:
                await update.message.reply_text(f"✅ Auto-renew cancelled. Your premium remains active until: {until}")
            else:
                await update.message.reply_text("✅ Auto-renew cancelled. Your premium remains active until the end of the current period.")
        else:
            await update.message.reply_text(f"❌ Cancel failed: {r.text}")
    except Exception as e:
        await update.message.reply_text(f"❌ Cancel error: {e}")

def start_bot_polling():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("cancel_autorenew", cmd_cancel_autorenew))
    print({"msg": "bot_start"})
    app.run_polling(allowed_updates=None, drop_pending_updates=False)

def start_alerts_loop():
    print({"msg": "alerts_loop_start", "interval": INTERVAL_SECONDS})
    init_db()
    while True:
        ts = datetime.utcnow().isoformat()
        try:
            with session_scope() as session:
                counters = run_alert_cycle(session)
            print({"msg": "alert_cycle", "ts": ts, **counters})
        except Exception as e:
            print({"msg": "alert_cycle_error", "ts": ts, "error": str(e)})
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    t_bot = threading.Thread(target=start_bot_polling, daemon=True)
    t_bot.start()
    start_alerts_loop()
