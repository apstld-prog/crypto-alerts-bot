# server_combined.py
# Single-process runner:
# - Telegram Bot (polling)
# - Alerts loop (background)
# - FastAPI health endpoints: /, /health, /botok, /alertsok

import os
import time
import threading
import re
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.error import Conflict
from telegram.constants import ParseMode

from sqlalchemy import select, text

from db import init_db, session_scope, User, Alert, Subscription, engine
from worker_logic import run_alert_cycle, resolve_symbol, fetch_price_binance

# ───────── ENV ─────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEB_URL = os.getenv("WEB_URL")
ADMIN_KEY = os.getenv("ADMIN_KEY")
INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))
FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT", "3"))
PAYPAL_PLAN_ID = os.getenv("PAYPAL_PLAN_ID")
PAYPAL_SUBSCRIBE_URL = os.getenv("PAYPAL_SUBSCRIBE_URL")
RUN_BOT = os.getenv("RUN_BOT", "1") == "1"
RUN_ALERTS = os.getenv("RUN_ALERTS", "1") == "1"
_ADMIN_IDS = {s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()}
BOT_LOCK_ID = int(os.getenv("BOT_LOCK_ID", "911001"))
ALERTS_LOCK_ID = int(os.getenv("ALERTS_LOCK_ID", "911002"))

# Health config
_BOT_HEART_INTERVAL = int(os.getenv("BOT_HEART_INTERVAL_SECONDS", "60"))
_BOT_HEART_TTL = int(os.getenv("BOT_HEART_TTL_SECONDS", "180"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

def is_admin(tg_id: str | None) -> bool:
    return (tg_id or "") in _ADMIN_IDS

# ───────── Advisory lock helpers ─────────
def try_advisory_lock(lock_id: int) -> bool:
    try:
        with engine.connect() as conn:
            res = conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}).scalar()
            return bool(res)
    except Exception as e:
        print({"msg": "advisory_lock_error", "lock_id": lock_id, "error": str(e)})
        return False

# ───────── Health server (FastAPI) ─────────
health_app = FastAPI()

# Shared state for health
_BOT_HEART_BEAT_AT: datetime | None = None
_BOT_HEART_STATUS: str = "unknown"  # ok/fail/unknown
_ALERTS_LAST_OK_AT: datetime | None = None
_ALERTS_LAST_RESULT: dict | None = None

@health_app.get("/")
def root():
    return {"ok": True, "service": "crypto-alerts-server"}

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
        "ttl_seconds": _BOT_HEART_TTL,
        "interval_seconds": _BOT_HEART_INTERVAL,
    }

@health_app.get("/alertsok")
def alertsok():
    return {
        "last_ok": (_ALERTS_LAST_OK_AT.isoformat() + "Z") if _ALERTS_LAST_OK_AT else None,
        "last_result": _ALERTS_LAST_RESULT or {},
        "expected_interval_seconds": INTERVAL_SECONDS,
    }

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    def _run():
        uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="info")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print({"msg": "health_server_started", "port": port})

def bot_heartbeat_loop():
    """Ping Telegram getMe περιοδικά – ενημερώνει cache για /botok."""
    global _BOT_HEART_BEAT_AT, _BOT_HEART_STATUS
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    print({"msg": "bot_heartbeat_started", "interval": _BOT_HEART_INTERVAL})
    while True:
        try:
            r = requests.get(url, timeout=10)
            ok = r.status_code == 200 and r.json().get("ok") is True
            _BOT_HEART_STATUS = "ok" if ok else "fail"
            _BOT_HEART_BEAT_AT = datetime.utcnow()
            if not ok:
                print({"msg": "bot_heartbeat_fail", "status": r.status_code, "body": r.text[:200]})
        except Exception as e:
            _BOT_HEART_STATUS = "fail"
            _BOT_HEART_BEAT_AT = datetime.utcnow()
            print({"msg": "bot_heartbeat_exception", "error": str(e)})
        time.sleep(_BOT_HEART_INTERVAL)

# ───────── Helpers ─────────
def target_msg(update: Update):
    return update.message or (update.callback_query.message if update.callback_query else None)

def paypal_upgrade_url_for(tg_id: str | None) -> str | None:
    if WEB_URL and PAYPAL_PLAN_ID and tg_id:
        return f"{WEB_URL}/billing/paypal/start?tg={tg_id}&plan_id={PAYPAL_PLAN_ID}"
    return PAYPAL_SUBSCRIBE_URL

def send_admins(text_msg: str) -> None:
    if not _ADMIN_IDS:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for admin_id in _ADMIN_IDS:
        if not admin_id:
            continue
        try:
            requests.post(url, json={"chat_id": admin_id, "text": text_msg}, timeout=10)
        except Exception:
            pass

def send_message(chat_id: str, text_msg: str) -> tuple[int, str]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text_msg}, timeout=15)
    return r.status_code, r.text

def op_from_rule(rule: str) -> str:
    return ">" if rule == "price_above" else "<"

def safe_chunks(s: str, limit: int = 3800):
    while s:
        yield s[:limit]
        s = s[limit:]

# ───────── UI Keyboards/Texts (όπως πριν) ─────────
# ... [κρατάω εδώ τα ίδια κομμάτια όπως στην προηγούμενη έκδοση που δουλεύει με commands]

# ───────── ΝΕΟ: cmd_cancel_autorenew ─────────
async def cmd_cancel_autorenew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel PayPal auto-renew (χρειάζεται WEB_URL + ADMIN_KEY στο ENV)."""
    if not WEB_URL or not ADMIN_KEY:
        await target_msg(update).reply_text("Cancel not available right now. Try again later.")
        return
    tg_id = str(update.effective_user.id)
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
                await target_msg(update).reply_text(
                    f"Auto-renew cancelled. Premium active until: {until}"
                )
            else:
                await target_msg(update).reply_text(
                    "Auto-renew cancelled. Premium remains active till end of period."
                )
        else:
            await target_msg(update).reply_text(f"Cancel failed: {r.text}")
    except Exception as e:
        await target_msg(update).reply_text(f"Cancel error: {e}")

# ───────── Alerts loop, delete_webhook, main() ─────────
# (ίδιο όπως στην έκδοση που σου έδωσα με health/botok/alertsok)

# μέσα στο main() βεβαιώσου ότι υπάρχει:
# app.add_handler(CommandHandler("cancel_autorenew", cmd_cancel_autorenew))
