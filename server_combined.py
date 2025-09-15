# server_combined.py
# Single process:
# - FastAPI health server
# - Telegram Bot (polling)
# - Alerts loop (background)
# - Extra features (Fear&Greed, Funding, Gainers/Losers, Chart, News, DCA, Pump alerts)
# - Free plan (10 alerts) vs Premium (unlimited), via plans.py

import os, time, threading, re
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

import requests
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse, PlainTextResponse, JSONResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.error import Conflict
from telegram.constants import ParseMode

from sqlalchemy import text
from db import init_db, session_scope, User, Subscription, engine
from worker_logic import run_alert_cycle, resolve_symbol, fetch_price_binance

# ---- Extra features ----
from commands_extra import register_extra_handlers
from worker_extra import start_pump_watcher
from models_extras import init_extras

# ---- Plans ----
from plans import build_plan_info, can_create_alert, plan_status_line

# ───────── ENV ─────────
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
WEB_URL = (os.getenv("WEB_URL") or "").strip() or None
ADMIN_KEY = (os.getenv("ADMIN_KEY") or "").strip() or None

INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))
FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT", "10"))

PAYPAL_PLAN_ID = (os.getenv("PAYPAL_PLAN_ID") or "").strip() or None
PAYPAL_SUBSCRIBE_URL = (os.getenv("PAYPAL_SUBSCRIBE_URL") or "").strip() or None

RUN_BOT = os.getenv("RUN_BOT", "1") == "1"
RUN_ALERTS = os.getenv("RUN_ALERTS", "1") == "1"

_ADMIN_IDS = {s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()}
BOT_LOCK_ID = int(os.getenv("BOT_LOCK_ID", "911001"))
ALERTS_LOCK_ID = int(os.getenv("ALERTS_LOCK_ID", "911002"))

# Heartbeat
_BOT_HEART_INTERVAL = int(os.getenv("BOT_HEART_INTERVAL_SECONDS", "60"))
_BOT_HEART_TTL = int(os.getenv("BOT_HEART_TTL_SECONDS", "180"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

def is_admin(tg_id: str | None) -> bool:
    return (tg_id or "") in _ADMIN_IDS

# ───────── Advisory lock helper ─────────
def acquire_advisory_lock(lock_id: int, name: str):
    try:
        conn = engine.connect()
        res = conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}).scalar()
        if res:
            print({"msg": "advisory_lock_acquired", "lock": name})
            return conn
        conn.close()
        return None
    except Exception as e:
        print({"msg": "advisory_lock_error", "error": str(e)})
        return None

# ───────── Health server ─────────
health_app = FastAPI()
_BOT_HEART_BEAT_AT = None
_BOT_HEART_STATUS = "unknown"
_ALERTS_LAST_OK_AT = None
_ALERTS_LAST_RESULT = None

@health_app.get("/")
def root():
    return {"ok": True}

@health_app.get("/health")
def health():
    return {"status": "ok"}

@health_app.get("/botok")
def botok():
    now = datetime.utcnow()
    stale = (_BOT_HEART_BEAT_AT is None) or ((now - _BOT_HEART_BEAT_AT) > timedelta(seconds=_BOT_HEART_TTL))
    return {
        "bot": ("stale" if stale else _BOT_HEART_STATUS),
        "last": (_BOT_HEART_BEAT_AT.isoformat() + "Z") if _BOT_HEART_BEAT_AT else None,
    }

@health_app.get("/alertsok")
def alertsok():
    return {
        "last_ok": (_ALERTS_LAST_OK_AT.isoformat() + "Z") if _ALERTS_LAST_OK_AT else None,
        "last_result": _ALERTS_LAST_RESULT or {},
    }

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    def _run():
        uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="info")
    threading.Thread(target=_run, daemon=True).start()

def bot_heartbeat_loop():
    global _BOT_HEART_BEAT_AT, _BOT_HEART_STATUS
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    while True:
        try:
            r = requests.get(url, timeout=10)
            ok = r.status_code == 200 and r.json().get("ok") is True
            _BOT_HEART_STATUS = "ok" if ok else "fail"
            _BOT_HEART_BEAT_AT = datetime.utcnow()
        except Exception:
            _BOT_HEART_STATUS = "fail"
            _BOT_HEART_BEAT_AT = datetime.utcnow()
        time.sleep(_BOT_HEART_INTERVAL)

# ───────── UI helpers ─────────
def target_msg(update: Update):
    return update.message or (update.callback_query.message if update.callback_query else None)

def paypal_upgrade_url_for(tg_id: str | None) -> str | None:
    if WEB_URL and (PAYPAL_PLAN_ID or PAYPAL_SUBSCRIBE_URL) and tg_id:
        return f"{WEB_URL}/billing/paypal/start?tg={tg_id}&plan_id={PAYPAL_PLAN_ID or ''}"
    return PAYPAL_SUBSCRIBE_URL

def main_menu_keyboard(tg_id: str | None):
    rows = [
        [InlineKeyboardButton("📊 Price BTC", callback_data="go:price:BTC"),
         InlineKeyboardButton("🔔 My Alerts", callback_data="go:myalerts")],
        [InlineKeyboardButton("⏱️ Set Alert Help", callback_data="go:setalerthelp"),
         InlineKeyboardButton("ℹ️ Help", callback_data="go:help")],
    ]
    u = paypal_upgrade_url_for(tg_id)
    if u:
        rows.append([InlineKeyboardButton("💎 Upgrade with PayPal", url=u)])
    return InlineKeyboardMarkup(rows)

def upgrade_keyboard(tg_id: str | None):
    u = paypal_upgrade_url_for(tg_id)
    return InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade with PayPal", url=u)]]) if u else None

def start_text(limit: int) -> str:
    return (
        "<b>Crypto Alerts Bot</b>\n"
        f"🆓 Free: up to {limit} alerts\n💎 Premium: unlimited alerts\n\n"
        "Commands: /price, /setalert, /myalerts, /help"
    )

# ───────── Commands ─────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    user_limit = 9999 if plan.has_unlimited else plan.free_limit
    await target_msg(update).reply_text(
        start_text(user_limit),
        reply_markup=main_menu_keyboard(tg_id),
        parse_mode=ParseMode.HTML,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    help_html = (
        "<b>Help</b>\n\n"
        "• /price <SYMBOL>\n"
        "• /setalert <SYMBOL> <op> <value>\n"
        "• /myalerts\n"
        "• /delalert <id> (Premium)\n"
        "• /clearalerts (Premium)\n"
        "• /cancel_autorenew\n"
        "• /support <msg>\n"
        "• /whoami\n"
        "• /requestcoin <SYMBOL>\n\n"
        "<b>Extra Features</b>\n"
        "• /feargreed\n"
        "• /funding [SYMBOL]\n"
        "• /topgainers, /toplosers\n"
        "• /chart <SYMBOL>\n"
        "• /news [N]\n"
        "• /dca <amount> <buys> <symbol>\n"
        "• /pumplive on|off [threshold%]\n"
    )
    await target_msg(update).reply_text(help_html, reply_markup=upgrade_keyboard(tg_id), parse_mode=ParseMode.HTML)

# (οι υπόλοιπες εντολές /whoami, /price, /setalert, /myalerts, admin, callbacks, loops κλπ παραμένουν ΟΠΩΣ στην προηγούμενη έκδοση που σου έδωσα)

def main():
    init_db()
    init_extras()
    start_health_server()
    threading.Thread(target=bot_heartbeat_loop, daemon=True).start()
    threading.Thread(target=alerts_loop, daemon=True).start()
    start_pump_watcher()
    run_bot()

if __name__ == "__main__":
    main()
