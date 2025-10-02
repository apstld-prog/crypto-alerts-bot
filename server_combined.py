import os
import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler
)

# === Logging ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# === FastAPI app ===
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok", "service": "crypto-alerts-bot"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/botok")
async def botok():
    return {"status": "bot alive"}

@app.get("/alertsok")
async def alertsok():
    return {"status": "alerts loop running"}

# === Telegram Bot Setup ===
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID", "")  # βάζεις το δικό σου Telegram user_id
TRIAL_DAYS = 10

# Memory DB (αντικαθίσταται με PostgreSQL στην πράξη)
USERS = {}  # {telegram_id: {"start_date": datetime, "premium_until": datetime or None}}

# === Commands ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.utcnow()

    if user_id not in USERS:
        USERS[user_id] = {"start_date": now, "premium_until": None}

    data = USERS[user_id]
    trial_end = data["start_date"] + timedelta(days=TRIAL_DAYS)

    # Admin έχει full
    if str(user_id) == str(ADMIN_ID):
        msg = "👑 Welcome admin!\nYou have unlimited access."
    else:
        msg = (
            f"👋 Welcome {update.effective_user.first_name}!\n"
            f"✅ You have full trial access for {TRIAL_DAYS} days.\n"
            f"🕒 Trial ends: {trial_end.strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            "After trial, contact admin to extend your access."
        )

    # Short menu summary
    msg += (
        "\n\n📌 Available commands:\n"
        "/price BTC — check live price\n"
        "/setalert BTC 50000 — set price alert\n"
        "/myalerts — manage alerts\n"
        "/help — instructions\n"
        "/support <msg> — contact admin"
    )

    await update.message.reply_text(msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 Bot Instructions\n\n"
        "/price <SYMBOL> → Get current price\n"
        "/setalert <SYMBOL> <VALUE> → Set price alert\n"
        "/myalerts → Manage your alerts\n"
        "/support <msg> → Send message to admin\n\n"
        "Extra features:\n"
        "/feargreed, /funding, /topgainers, /toplosers, /chart SYMBOL\n"
        "/news SYMBOL, /dca <amount> <buys> <symbol>, /pumpvline\n"
        "/alts SYMBOL, /listalts\n"
    )
    await update.message.reply_text(text)

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /support <your message>")
        return
    msg = " ".join(context.args)
    await update.message.reply_text("✅ Your message has been sent to admin.")
    if ADMIN_ID:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"[Support] {update.effective_user.id}: {msg}")

# === Alerts cycle placeholder ===
async def alerts_loop():
    while True:
        logger.info({"msg": "alert_cycle", "ts": datetime.utcnow().isoformat()})
        await asyncio.sleep(60)

# === Main run ===
async def run_bot():
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("support", support))

    # Run bot polling in background
    asyncio.create_task(application.run_polling())

async def main():
    logger.info("Starting Crypto Alerts Bot service...")

    # Start Telegram bot
    asyncio.create_task(run_bot())

    # Start alerts loop
    asyncio.create_task(alerts_loop())

    # Start FastAPI (port from Render)
    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
