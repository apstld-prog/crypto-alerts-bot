import os
import logging
import asyncio
from datetime import datetime, timedelta

from fastapi import FastAPI
import uvicorn
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters
)

# ==========================================
# CONFIG
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5254014824"))  # βάλε το δικό σου Telegram ID
TRIAL_DAYS = 10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory "DB" για demo. Στη πράξη → PostgreSQL
users_db = {}  # { user_id: {trial_end, premium} }

# ==========================================
# UTILS
# ==========================================
def get_user(user_id: int):
    """Βρες ή φτιάξε χρήστη"""
    if user_id not in users_db:
        users_db[user_id] = {
            "trial_end": datetime.utcnow() + timedelta(days=TRIAL_DAYS),
            "premium": False
        }
    return users_db[user_id]


def is_admin(user_id: int):
    return user_id == ADMIN_ID


def user_active(user_id: int):
    u = get_user(user_id)
    if u["premium"]:
        return True
    return datetime.utcnow() <= u["trial_end"]


# ==========================================
# HANDLERS
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)

    if is_admin(uid):
        await update.message.reply_text("👑 Welcome Admin — you have unlimited access.")
        return

    trial_left = (u["trial_end"] - datetime.utcnow()).days
    await update.message.reply_text(
        f"👋 Welcome {update.effective_user.first_name}!\n\n"
        f"✅ You have FULL access for {TRIAL_DAYS} days.\n"
        f"📅 Trial ends on: {u['trial_end'].strftime('%Y-%m-%d')}\n"
        f"⏳ Days left: {max(0, trial_left)}\n\n"
        f"After trial ends, contact admin to extend your access."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📘 *Bot Commands*\n\n"
        "General:\n"
        "/price BTC → Get live price\n"
        "/setalert BTC > 50000 → Create alert\n"
        "/myalerts → List alerts\n"
        "/feargreed → Fear & Greed Index\n"
        "/chart BTC → Show chart\n"
        "/alts SYMBOL → Curated token info\n"
        "/listalts, /listpresales → Token lists\n"
        "/support <msg> → Contact support\n\n"
        "⚡ Trial: 10 days full access to everything.\n"
        "👑 After trial → contact admin."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not user_active(uid) and not is_admin(uid):
        await update.message.reply_text("⛔ Trial expired. Contact admin to extend access.")
        return
    # εδώ placeholder — βάλε API Binance/CMC
    await update.message.reply_text("BTC price = 68,000 USDT")


# ==========================================
# ADMIN COMMANDS
# ==========================================
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = "👥 Users:\n"
    for uid, data in users_db.items():
        msg += f"• {uid} | premium={data['premium']} | trial_end={data['trial_end']}\n"
    await update.message.reply_text(msg)


async def extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        days = int(context.args[0])
        uid = int(context.args[1])
        u = get_user(uid)
        u["trial_end"] += timedelta(days=days)
        await update.message.reply_text(f"✅ Extended {uid} by {days} days.")
    except Exception as e:
        await update.message.reply_text(f"Usage: /extend <days> <user_id>\nError: {e}")


async def setpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        uid = int(context.args[0])
        u = get_user(uid)
        u["premium"] = True
        await update.message.reply_text(f"✅ User {uid} upgraded to PREMIUM.")
    except Exception as e:
        await update.message.reply_text(f"Usage: /setpremium <user_id>\nError: {e}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total = len(users_db)
    premiums = sum(1 for u in users_db.values() if u["premium"])
    await update.message.reply_text(f"📊 Users: {total}\n⭐ Premium: {premiums}")


# ==========================================
# MAIN
# ==========================================
def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price))

    # Admin commands
    app.add_handler(CommandHandler("listusers", list_users))
    app.add_handler(CommandHandler("extend", extend))
    app.add_handler(CommandHandler("setpremium", setpremium))
    app.add_handler(CommandHandler("stats", stats))

    app.run_polling()


# FastAPI healthcheck
fastapi_app = FastAPI()

@fastapi_app.get("/")
async def root():
    return {"msg": "Bot alive"}

@fastapi_app.get("/health")
async def health():
    return {"status": "ok"}


def main():
    loop = asyncio.get_event_loop()
    loop.create_task(asyncio.to_thread(run_bot))
    uvicorn.run(fastapi_app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))


if __name__ == "__main__":
    main()
