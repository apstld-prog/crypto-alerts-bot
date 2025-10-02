# server_combined.py
import os
import threading
import time
import logging
from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# ────────────────────────── Config ──────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("server_combined")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # βάλε το δικό σου Telegram user id
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "10"))
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

# ───────────────── In-memory store (demo) ───────────────────
# Σε παραγωγή αντικαθίσταται με DB (Postgres).
users_db = {}  # { user_id: {"trial_end": datetime, "premium": bool} }

def get_user(uid: int):
    if uid not in users_db:
        users_db[uid] = {
            "trial_end": datetime.utcnow() + timedelta(days=TRIAL_DAYS),
            "premium": False,
        }
    return users_db[uid]

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def has_access(uid: int) -> bool:
    if is_admin(uid):
        return True
    u = get_user(uid)
    if u["premium"]:
        return True
    return datetime.utcnow() <= u["trial_end"]

# ──────────────────────── Telegram Bot ───────────────────────
_bot_app = None
_bot_started = False
_last_alerts_ok = None
_last_bot_ok = None

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)

    if is_admin(uid):
        await update.message.reply_text("👑 Welcome Admin — unlimited access enabled.")
        return

    left = u["trial_end"] - datetime.utcnow()
    days_left = max(0, left.days)
    msg = (
        f"👋 Welcome {update.effective_user.first_name}!\n\n"
        f"✅ Full access for {TRIAL_DAYS} days.\n"
        f"📅 Trial ends on: {u['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"⏳ Days left: {days_left}\n\n"
        "After the trial, contact admin to extend your access.\n\n"
        "📌 Commands:\n"
        "• /price BTC — live price (demo)\n"
        "• /setalert BTC > 50000 — create alert (demo)\n"
        "• /myalerts — list alerts (demo)\n"
        "• /help — instructions\n"
        "• /support <msg> — contact admin\n"
    )
    await update.message.reply_text(msg)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📘 Bot Commands\n\n"
        "/price <SYMBOL> → Get live price (demo)\n"
        "/setalert <SYMBOL> <op> <value> → Set alert (demo)\n"
        "/myalerts → Your alerts (demo)\n"
        "/support <msg> → Send message to admin\n\n"
        "Trial: 10 days full access to everything.\n"
        "After trial → contact admin to extend access."
    )
    await update.message.reply_text(text)

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /support <your message>")
        return
    msg = " ".join(context.args)
    await update.message.reply_text("✅ Your message was sent to admin.")
    if ADMIN_ID:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"[Support] from {update.effective_user.id}: {msg}")

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_access(uid):
        await update.message.reply_text("⛔ Trial expired. Contact admin to extend access.")
        return
    # Demo τιμή. Ενσωμάτωσε Binance/CMC εδώ αν θες.
    await update.message.reply_text("BTC price ≈ 68000 USDT (demo)")

# ─────────────── Admin Commands (μόνο για σένα) ─────────────
async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not users_db:
        await update.message.reply_text("📭 No users yet.")
        return
    lines = ["👥 Users:"]
    for uid, data in users_db.items():
        lines.append(
            f"• {uid} | premium={data['premium']} | trial_end={data['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}"
        )
    await update.message.reply_text("\n".join(lines))

async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /extend <days> <user_id>")
        return
    try:
        days = int(context.args[0])
        uid = int(context.args[1])
    except Exception as e:
        await update.message.reply_text(f"Usage: /extend <days> <user_id>\nError: {e}")
        return
    u = get_user(uid)
    u["trial_end"] += timedelta(days=days)
    await update.message.reply_text(f"✅ Extended {uid} by {days} day(s).")

async def cmd_setpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /setpremium <user_id>")
        return
    try:
        uid = int(context.args[0])
    except Exception as e:
        await update.message.reply_text(f"Usage: /setpremium <user_id>\nError: {e}")
        return
    u = get_user(uid)
    u["premium"] = True
    await update.message.reply_text(f"👑 {uid} is now PREMIUM.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total = len(users_db)
    premiums = sum(1 for v in users_db.values() if v["premium"])
    await update.message.reply_text(f"📊 Users: {total}\n⭐ Premium: {premiums}")

def _bot_thread():
    """Runs the Telegram bot in polling mode (single instance)"""
    global _bot_app, _last_bot_ok
    log.info("Starting Telegram bot polling…")
    try:
        _bot_app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .read_timeout(40)
            .connect_timeout(15)
            .build()
        )

        # User commands
        _bot_app.add_handler(CommandHandler("start", cmd_start))
        _bot_app.add_handler(CommandHandler("help", cmd_help))
        _bot_app.add_handler(CommandHandler("support", cmd_support))
        _bot_app.add_handler(CommandHandler("price", cmd_price))

        # Admin-only commands (ελέγχονται μέσα στους handlers)
        _bot_app.add_handler(CommandHandler("listusers", cmd_listusers))
        _bot_app.add_handler(CommandHandler("extend", cmd_extend))
        _bot_app.add_handler(CommandHandler("setpremium", cmd_setpremium))
        _bot_app.add_handler(CommandHandler("stats", cmd_stats))

        _bot_app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        log.exception("Bot polling crashed: %s", e)
    finally:
        _last_bot_ok = datetime.utcnow()

def _alerts_thread():
    """Dummy alerts loop για health /alertsok (βάλε τη δική σου λογική εδώ)."""
    global _last_alerts_ok
    log.info("Starting alerts loop…")
    while True:
        try:
            # εδώ θα καλούσες run_alert_cycle(...)
            _last_alerts_ok = datetime.utcnow()
            time.sleep(60)
        except Exception as e:
            log.error("alerts_loop error: %s", e)
            time.sleep(60)

# ─────────────────────── FastAPI (health_app) ───────────────────────
health_app = FastAPI(title="Crypto Alerts Health")

@health_app.on_event("startup")
def on_startup():
    # Ξεκίνησε bot + alerts σε background threads όταν σηκώνεται το ASGI app
    global _bot_started
    if not _bot_started:
        threading.Thread(target=_bot_thread, daemon=True).start()
        threading.Thread(target=_alerts_thread, daemon=True).start()
        _bot_started = True
        log.info("Background workers started (bot + alerts)")

@health_app.api_route("/", methods=["GET", "HEAD"])
def root():
    return JSONResponse({"ok": True, "service": "crypto-alerts-bot"})

@health_app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return JSONResponse({"status": "ok", "time": datetime.utcnow().isoformat() + "Z"})

@health_app.api_route("/botok", methods=["GET", "HEAD"])
def botok():
    status = "running" if _bot_started else "not_started"
    return JSONResponse({"bot": status})

@health_app.api_route("/alertsok", methods=["GET", "HEAD"])
def alertsok():
    ts = _last_alerts_ok.isoformat() + "Z" if _last_alerts_ok else None
    return JSONResponse({"last_ok": ts})

# ───────────────────────── Local run helper ─────────────────────────
def main():
    uvicorn.run(health_app, host="0.0.0.0", port=PORT, log_level="info")

if __name__ == "__main__":
    main()
