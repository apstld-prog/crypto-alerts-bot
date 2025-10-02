# server_combined.py
# FastAPI + python-telegram-bot (v20+) with:
# - async polling (inside FastAPI lifecycle)
# - deleteWebhook safeguard
# - 10-day trial from /start (in-memory)
# - Admin tools: /extend <days> <user_id>, /setpremium <user_id>, /listusers, /stats
# - Global access guard (trial/premium/admin) that DOES NOT change legacy handlers logic
# - No PayPal references

import os
import logging
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler, ContextTypes,
    filters, ApplicationHandlerStop
)

# ────────────────────────── Config / Logging ──────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s"
)
log = logging.getLogger("server_combined")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "10"))

if not BOT_TOKEN:
    raise RuntimeError("Missing token: set BOT_TOKEN or TELEGRAM_TOKEN")

# ───────────────── In-memory Access Store (Trial/Premium) ─────────────
_users = {}  # { uid: {"trial_end": datetime, "premium": bool} }

def _get_user(uid: int):
    if uid not in _users:
        _users[uid] = {
            "trial_end": datetime.utcnow() + timedelta(days=TRIAL_DAYS),
            "premium": False,
        }
    return _users[uid]

def _is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def _has_access(uid: int) -> bool:
    if _is_admin(uid):
        return True
    u = _get_user(uid)
    return u["premium"] or (datetime.utcnow() <= u["trial_end"])

# ──────────────────────── Rich texts & keyboard ──────────────────────
def _reply_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("/price BTC"), KeyboardButton("/myalerts")],
        [KeyboardButton("/help"), KeyboardButton("/support I need help")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def _features_block() -> str:
    # Μόνο ενημερωτικό κείμενο. Δεν αλλάζει τη λειτουργία των legacy commands.
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

def _start_text(uid: int, first: str) -> str:
    u = _get_user(uid)
    left_days = max(0, (u["trial_end"] - datetime.utcnow()).days)
    return (
        f"👋 Welcome {first.upper()}!\n\n"
        f"✅ Full access for {TRIAL_DAYS} days.\n"
        f"📅 Trial ends on: {u['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"⏳ Days left: {left_days}\n\n"
        "After the trial, contact admin to extend your access.\n\n"
        "📌 Commands:\n" + _features_block()
    )

# ──────────────────────────── Guard & Basics ──────────────────────────
ALWAYS_ALLOWED = {
    "start", "help", "whoami", "support", "ping",
    "stats", "listusers", "extend", "setpremium"  # admin tools
}

async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global trial/premium gate. Δεν αλλάζει τις εντολές σου·
    απλώς τις αφήνει να περάσουν αν ο χρήστης έχει πρόσβαση."""
    msg = update.effective_message
    if not msg or not msg.text or not msg.text.startswith("/"):
        return
    uid = update.effective_user.id if update.effective_user else 0
    cmd = msg.text.split()[0].split("@")[0][1:].lower()

    if cmd in ALWAYS_ALLOWED or _is_admin(uid) or _has_access(uid):
        return

    await msg.reply_text("⛔ Το δοκιμαστικό σου έληξε. Στείλε /support για επέκταση από admin.")
    raise ApplicationHandlerStop

# Μικρές βασικές (δεν πειράζουν legacy)
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = _get_user(uid)
    role = "admin" if _is_admin(uid) else "user"
    active = _has_access(uid)
    await update.message.reply_text(
        f"Role: {role}\nActive access: {active}\n"
        f"Trial end: {u['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Premium: {u['premium']}"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    kb = _reply_kb()
    if _is_admin(uid):
        await update.message.reply_text("👑 Welcome Admin — unlimited access.\n\n" + _features_block(), reply_markup=kb)
        return
    await update.message.reply_text(_start_text(uid, update.effective_user.first_name or "User"), reply_markup=kb)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_features_block(), reply_markup=_reply_kb())

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /support <your message>")
        return
    msg = " ".join(context.args)
    await update.message.reply_text("✅ Your message was sent to admin.")
    if ADMIN_ID:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"[Support] from {update.effective_user.id}: {msg}")

# ───────────────────────── Admin tools ────────────────────────────────
async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if not _users:
        await update.message.reply_text("📭 No users yet.")
        return
    lines = ["👥 Users:"]
    for uid, data in _users.items():
        lines.append(
            f"• {uid} | premium={data['premium']} | trial_end={data['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}"
        )
    await update.message.reply_text("\n".join(lines))

async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /extend <days> <user_id>")
        return
    try:
        days = int(context.args[0]); uid = int(context.args[1])
    except Exception as e:
        await update.message.reply_text(f"Usage: /extend <days> <user_id>\nError: {e}")
        return
    u = _get_user(uid); u["trial_end"] += timedelta(days=days)
    await update.message.reply_text(
        f"✅ Extended {uid} by {days} day(s). New end: {u['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}"
    )

async def cmd_setpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /setpremium <user_id>")
        return
    try:
        uid = int(context.args[0])
    except Exception as e:
        await update.message.reply_text(f"Usage: /setpremium <user_id>\nError: {e}")
        return
    u = _get_user(uid); u["premium"] = True
    await update.message.reply_text(f"👑 {uid} is now PREMIUM.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    total = len(_users)
    premiums = sum(1 for v in _users.values() if v["premium"])
    await update.message.reply_text(f"📊 Users: {total}\n⭐ Premium: {premiums}")

# ─────────────────────── Telegram startup helpers ─────────────────────
async def _delete_webhook_async():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        log.info("deleteWebhook status=%s body=%s", r.status_code, r.text[:160])
    except Exception as e:
        log.warning("deleteWebhook error: %s", e)

async def _get_me_async():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        data = r.json()
        if data.get("ok"):
            log.info("getMe ok: id=%s username=@%s", data["result"].get("id"), data["result"].get("username"))
        else:
            log.error("getMe failed: %s", r.text)
    except Exception as e:
        log.error("getMe exception: %s", e)

# ───────────────────── Legacy handlers registration ───────────────────
def register_legacy_handlers(app: Application):
    """
    Δηλώνουμε τα ΠΑΛΙΑ σου handlers όπως ήσαν.
    Κάνουμε safe import: αν κάποιο όνομα λείπει, γράφουμε warning,
    αλλά δεν σταματά η εκκίνηση.
    """
    def _try(cmd: str, func_name: str):
        try:
            # προσπαθούμε να βρούμε τη function στο global namespace (ίδιο αρχείο)
            fn = globals().get(func_name)
            if fn is None:
                # αν οι παλιές functions ζουν σε άλλα modules, κάνε εδώ import
                # π.χ.: from worker_logic import cmd_price  (αν χρειάζεται)
                # Εμείς δοκιμάζουμε common modules που είχες ήδη.
                import importlib
                for mod in ("server_combined", "worker_logic", "worker", "commands_extra", "features_market"):
                    try:
                        m = importlib.import_module(mod)
                        fn = getattr(m, func_name, None)
                        if fn:
                            break
                    except Exception:
                        continue
            if fn is None:
                log.warning("Legacy handler missing: %s -> %s (skipped)", cmd, func_name)
                return
            app.add_handler(CommandHandler(cmd, fn), group=1)
            log.info("Legacy handler registered: /%s -> %s", cmd, func_name)
        except Exception as e:
            log.warning("Failed to register /%s -> %s : %s", cmd, func_name, e)

    # ➜ Δήλωσε εδώ ΟΠΩΣ ήταν τα ονόματα των functions σου
    _try("price", "cmd_price")
    _try("setalert", "cmd_setalert")
    _try("myalerts", "cmd_myalerts")
    _try("delalert", "cmd_delalert")
    _try("clearalerts", "cmd_clearalerts")
    _try("cancel_autorenew", "cmd_cancel_autorenew")

    _try("feargreed", "cmd_feargreed")
    _try("funding", "cmd_funding")
    _try("topgainers", "cmd_topgainers")
    _try("toplosers", "cmd_toplosers")
    _try("chart", "cmd_chart")
    _try("news", "cmd_news")
    _try("dca", "cmd_dca")
    _try("pumplive", "cmd_pumplive")

    _try("listalts", "cmd_listalts")
    _try("listpresales", "cmd_listpresales")
    _try("alts", "cmd_alts")

# ───────────────────────── FastAPI / health_app ───────────────────────
health_app = FastAPI(title="Crypto Alerts Health")
application: Application | None = None
_bot_started = False

@health_app.on_event("startup")
async def on_startup():
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

    # 1) Global guard ΠΡΙΝ από όλα
    application.add_handler(MessageHandler(filters.COMMAND, access_guard), group=0)

    # 2) Βασικές/adm μικρές
    application.add_handler(CommandHandler("ping", cmd_ping), group=1)
    application.add_handler(CommandHandler("whoami", cmd_whoami), group=1)
    application.add_handler(CommandHandler("start", cmd_start), group=1)
    application.add_handler(CommandHandler("help", cmd_help), group=1)
    application.add_handler(CommandHandler("support", cmd_support), group=1)
    application.add_handler(CommandHandler("listusers", cmd_listusers), group=1)
    application.add_handler(CommandHandler("extend", cmd_extend), group=1)
    application.add_handler(CommandHandler("setpremium", cmd_setpremium), group=1)
    application.add_handler(CommandHandler("stats", cmd_stats), group=1)

    # 3) Legacy registrations (όπως ήταν χθες)
    register_legacy_handlers(application)

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
    return JSONResponse({"bot": "running" if _bot_started else "not_started"})

@health_app.api_route("/alertsok", methods=["GET", "HEAD"])
async def alertsok():
    return JSONResponse({"last_ok": None})
