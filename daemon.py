
import os, time, threading, re
from datetime import datetime
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from sqlalchemy import select, text
from db import init_db, session_scope, User, Alert
from worker_logic import run_alert_cycle, resolve_symbol, fetch_price_binance

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_URL = os.getenv("WEB_URL")
ADMIN_KEY = os.getenv("ADMIN_KEY")
INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS","60"))
PAYPAL_SUBSCRIBE_URL = os.getenv("PAYPAL_SUBSCRIBE_URL")
FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT","3"))

HELP_TEXT = "Commands: /price, /setalert, /myalerts, /cancel_autorenew"

def upgrade_keyboard():
    if PAYPAL_SUBSCRIBE_URL:
        return InlineKeyboardMarkup([[InlineKeyboardButton("💠 Upgrade with PayPal", url=PAYPAL_SUBSCRIBE_URL)]])
    return None

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id==tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
            session.add(user)
            session.flush()
    await update.message.reply_text("Welcome! Use /help.", reply_markup=upgrade_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=upgrade_keyboard())

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price BTC")
        return
    pair = resolve_symbol(context.args[0])
    if not pair:
        await update.message.reply_text("Unknown symbol")
        return
    price = fetch_price_binance(pair)
    await update.message.reply_text(f"{pair} = {price} USDT")

ALERT_RE = re.compile(r"^(?P<sym>\w+)\s*(?P<op>>|<)\s*(?P<val>[0-9]+(\.[0-9]+)?)$")

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setalert BTC > 30000")
        return
    m = ALERT_RE.match(" ".join(context.args))
    if not m:
        await update.message.reply_text("Format error")
        return
    sym, op, val = m.group("sym"), m.group("op"), float(m.group("val"))
    pair = resolve_symbol(sym)
    rule = "price_above" if op==">" else "price_below"
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id==tg_id)).scalar_one_or_none()
        if not user: return
        active_alerts = session.execute(text("SELECT COUNT(*) FROM alerts WHERE user_id=:uid"),{"uid":user.id}).scalar_one()
        if not user.is_premium and active_alerts>=FREE_ALERT_LIMIT:
            await update.message.reply_text("Free limit reached. Upgrade!")
            return
        alert = Alert(user_id=user.id, symbol=pair, rule=rule, value=val, cooldown_seconds=900)
        session.add(alert); session.flush()
        aid = alert.id
    await update.message.reply_text(f"Alert #{aid} set.")

def alerts_loop():
    while True:
        with session_scope() as session:
            run_alert_cycle(session)
        time.sleep(INTERVAL_SECONDS)

def main():
    threading.Thread(target=alerts_loop,daemon=True).start()
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    print("Bot started")
    app.run_polling()

if __name__=="__main__": main()
