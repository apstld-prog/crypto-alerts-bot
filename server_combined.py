# server_combined.py
import os
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes
)

# ────────────────────────── Config / Logging ──────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("server_combined")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))         # βάλε το δικό σου Telegram user id
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "10"))

if not BOT_TOKEN:
    raise RuntimeError("Missing token: set BOT_TOKEN or TELEGRAM_TOKEN in environment")

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

# ──────────────────────── Telegram Bot (async) ───────────────────────
application: Application | None = None
_bot_started = False
_bot_username: str | None = None

async def _delete_webhook_async():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        log.info("deleteWebhook status=%s body=%s", r.status_code, r.text[:160])
    except Exception as e:
        log.warning("deleteWebhook error: %s", e)

async def _get_me_async():
    global _bot_username
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        data = r.json()
        if data.get("ok"):
            _bot_username = data["result"].get("username")
            log.info("getMe ok: id=%s username=@%s", data["result"].get("id"), _bot_username)
            return True
        log.error("getMe failed: %s", r.text)
        return False
    except Exception as e:
        log.error("getMe exception: %s", e)
        return False

# ─────────────── Helpers (texts & keyboard) ───────────────
def build_reply_keyboard() -> ReplyKeyboardMarkup:
    # Τα κουμπιά στέλνουν απευθείας τις εντολές ως κείμενο
    rows = [
        [KeyboardButton("/price BTC"), KeyboardButton("/myalerts")],
        [KeyboardButton("/help"), KeyboardButton("/support I need help")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)

def build_features_block() -> str:
    return (
        "Crypto Alerts Bot\n"
        "⚡ Fast prices • 🧪 Diagnostics • 🔔 Alerts\n\n"
        "Getting Started\n"
        "• /price BTC — current price\n"
        "• /setalert BTC > 110000 — alert when condition is met\n"
        "• /myalerts — list your active alerts (with delete buttons)\n"
        "• /help — instructions\n"
        "• /support <message> — contact admin support\n\n"
        "💎 Premium: unlimited alerts\n"
        "🆓 Free: up to 10 alerts.\n\n"
        "Extra Features\n"
        "• /feargreed → current Fear & Greed Index\n"
        "• /funding [SYMBOL] → futures funding rate or top extremes\n"
        "• /topgainers, /toplosers → 24h movers\n"
        "• /chart <SYMBOL> → mini chart (24h)\n"
        "• /news [N] → latest crypto headlines\n"
        "• /dca <amount_per_buy> <buys> <symbol>\n"
        "• /pumplive on|off [threshold%] → live pump alerts opt-in\n"
        "• /listalts, /listpresales, /alts <SYMBOL>\n\n"
        "🌱 New & Off-Binance — Try /alts HYPER or /alts OZ for info.\n"
        "If a token gets listed on Binance later, /price will auto-detect it."
    )

def build_start_text(uid: int, first_name: str) -> str:
    u = get_user(uid)
    left = u["trial_end"] - datetime.utcnow()
    days_left = max(0, left.days)
    header = (
        f"👋 Welcome {first_name.upper()}!\n\n"
        f"✅ Full access for {TRIAL_DAYS} days.\n"
        f"📅 Trial ends on: {u['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"⏳ Days left: {days_left}\n\n"
        "After the trial, contact admin to extend your access.\n\n"
        "📌 Commands:\n"
    )
    return header + build_features_block()

# ─────────────── User Commands ───────────────
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    role = "admin" if is_admin(uid) else "user"
    active = has_access(uid)
    await update.message.reply_text(
        f"Role: {role}\nActive access: {active}\n"
        f"Trial end: {u['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Premium: {u['premium']}"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    kb = build_reply_keyboard()

    if is_admin(uid):
        await update.message.reply_text(
            f"👑 Welcome Admin — unlimited access.\nBot: @{_bot_username or 'unknown'}\n\n" +
            build_features_block(),
            reply_markup=kb,
        )
        return

    txt = build_start_text(uid, update.effective_user.first_name or "User")
    await update.message.reply_text(txt, reply_markup=kb)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_features_block(), reply_markup=build_reply_keyboard())

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

# ─────────────── Admin Commands ───────────────
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
    await update.message.reply_text(f"📊 Users: {total}\n⭐ Premium: {premiums}\n🤖 Bot: @{_bot_username or 'unknown'}")

# ─────────────────────── FastAPI (health_app) ───────────────────────
health_app = FastAPI(title="Crypto Alerts Health")

@health_app.on_event("startup")
async def on_startup():
    """Ξεκινάει τον Telegram bot σε async mode στο ίδιο event loop του Uvicorn."""
    global application, _bot_started

    if _bot_started:
        return

    await _delete_webhook_async()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(40)
        .connect_timeout(15)
        .build()
    )

    # User commands
    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("support", cmd_support))
    application.add_handler(CommandHandler("price", cmd_price))

    # Admin commands
    application.add_handler(CommandHandler("listusers", cmd_listusers))
    application.add_handler(CommandHandler("extend", cmd_extend))
    application.add_handler(CommandHandler("setpremium", cmd_setpremium))
    application.add_handler(CommandHandler("stats", cmd_stats))

    await application.initialize()
    await application.start()
    await _get_me_async()
    await application.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

    _bot_started = True
    log.info("Telegram bot started (async polling).")

@health_app.on_event("shutdown")
async def on_shutdown():
    global application, _bot_started
    if application:
        try:
            await application.updater.stop()
        except Exception:
            pass
        try:
            await application.stop()
        except Exception:
            pass
        try:
            await application.shutdown()
        except Exception:
            pass
    _bot_started = False
    log.info("Telegram bot stopped.")

# Health endpoints
@health_app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return JSONResponse({"ok": True, "service": "crypto-alerts-bot"})

@health_app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return JSONResponse({"status": "ok", "time": datetime.utcnow().isoformat() + "Z"})

@health_app.api_route("/botok", methods=["GET", "HEAD"])
async def botok():
    status = "running" if _bot_started else "not_started"
    return JSONResponse({"bot": status, "username": _bot_username})

@health_app.api_route("/alertsok", methods=["GET", "HEAD"])
async def alertsok():
    return JSONResponse({"last_ok": None})
