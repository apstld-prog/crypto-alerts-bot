# server_combined.py
from __future__ import annotations

import os
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from sqlalchemy import text

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from db import session_scope, init_db, User  # Alert/Subscription tables exist in your project
from commands_admin import register_admin_handlers  # alias kept for compatibility

# -------------------- ENV --------------------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_KEY = (os.getenv("ADMIN_KEY") or "").strip() or None
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "10"))
WORKER_INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))  # if you need background loops later

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

# -------------------- FastAPI app --------------------
app = FastAPI(title="Crypto Alerts Server")

@app.get("/", response_class=JSONResponse)
def root():
    return {"ok": True, "service": "crypto-alerts-combined", "time": datetime.utcnow().isoformat()}

@app.get("/health", response_class=JSONResponse)
def health():
    return {"status": "ok"}

@app.get("/botok", response_class=JSONResponse)
def botok():
    alive = _bot_is_running()
    return {"bot_running": alive, "ts": datetime.utcnow().isoformat()}

@app.get("/alertsok", response_class=PlainTextResponse)
def alertsok():
    # simple placeholder; extend with your own alert loop checks
    return "ok"

# -------------------- Telegram Bot (PTB v20+) --------------------
tg_app: Optional[Application] = None

def _admin_ids() -> set[str]:
    if not ADMIN_KEY:
        return set()
    return {p.strip() for p in ADMIN_KEY.split(",") if p.strip()}

def _bot_is_running() -> bool:
    return bool(tg_app and tg_app.running)

def _trial_status_line_for(tg_id: str | None) -> str:
    if not tg_id:
        return "Trial: unknown user"
    with session_scope() as session:
        row = session.execute(text(
            """
            SELECT s.provider_sub_id
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE u.telegram_id = :tg AND s.provider = 'trial'
            ORDER BY s.created_at DESC
            LIMIT 1
            """
        ), {"tg": tg_id}).mappings().first()
        if not row or not row.get("provider_sub_id"):
            return "Trial: no active trial — contact admin"
        try:
            expiry = datetime.fromisoformat(row["provider_sub_id"])
            now = datetime.now(timezone.utc)
            if expiry > now:
                return f"Trial expires: {expiry.date().isoformat()}"
            return "Trial: expired — contact admin"
        except Exception:
            return "Trial: unknown — contact admin"

def main_menu_keyboard(tg_id: str | None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Price BTC", callback_data="go:price:BTC"),
         InlineKeyboardButton("🔔 My Alerts", callback_data="go:myalerts")],
        [InlineKeyboardButton("⏱️ Set Alert Help", callback_data="go:setalerthelp"),
         InlineKeyboardButton("ℹ️ Help", callback_data="go:help")],
        [InlineKeyboardButton("🆘 Support", callback_data="go:support")],
        [InlineKeyboardButton(_trial_status_line_for(tg_id), callback_data="noop:trial")]
    ]
    return InlineKeyboardMarkup(rows)

def start_text() -> str:
    return (
        "<b>Crypto Alerts Bot</b>\n"
        "⚡ Fast prices • 🔔 Alerts • 🧪 Diagnostics\n\n"
        "<b>Getting Started</b>\n"
        "• <code>/price BTC</code> — current price\n"
        "• <code>/setalert BTC &gt; 110000</code> — alert when condition is met\n"
        "• <code>/myalerts</code> — list your active alerts (with delete buttons)\n"
        "• <code>/help</code> — instructions\n"
        "• <code>/support &lt;message&gt;</code> — contact admin support\n\n"
        "🎁 <b>Trial</b>: 10 days with full/unlimited access.\n"
        "After trial expires, contact the admin to extend access.\n"
    )

async def _ensure_trial_row(user_id: int) -> str:
    """
    Creates a 10-day trial if none exists or informs if trial already existed/expired.
    Returns a message line to append to /start response.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        row = session.execute(text(
            "SELECT provider_sub_id FROM subscriptions WHERE user_id = :uid AND provider = 'trial' "
            "ORDER BY created_at DESC LIMIT 1"
        ), {"uid": user_id}).mappings().first()

        if row:
            expiry_iso = row.get("provider_sub_id")
            try:
                expiry = datetime.fromisoformat(expiry_iso) if expiry_iso else None
            except Exception:
                expiry = None

            if expiry and expiry > now:
                days_left = (expiry - now).days
                return f"\n\n✅ You already have an active free trial for {days_left} more day(s)."
            else:
                return "\n\n⚠️ Your previous free trial has expired. To get more days, please contact the admin."
        else:
            expiry = now + timedelta(days=TRIAL_DAYS)
            session.execute(text(
                "INSERT INTO subscriptions (user_id, provider, provider_sub_id, status_internal, created_at, updated_at) "
                "VALUES (:uid, 'trial', :expiry, 'active', NOW(), NOW())"
            ), {"uid": user_id, "expiry": expiry.isoformat()})
            return f"\n\n🎁 You received a free {TRIAL_DAYS}-day trial with full access. It will expire on {expiry.date().isoformat()} (UTC)."

# -------------------- Command Handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    tg_id = str(tg_user.id) if tg_user else None

    # ensure user row
    with session_scope() as session:
        user = session.execute(text("SELECT id FROM users WHERE telegram_id=:tg"), {"tg": tg_id}).mappings().first()
        if not user:
            session.execute(text("INSERT INTO users (telegram_id, is_premium, created_at, updated_at) "
                                 "VALUES (:tg, FALSE, NOW(), NOW())"), {"tg": tg_id})
            # fetch id
            user = session.execute(text("SELECT id FROM users WHERE telegram_id=:tg"), {"tg": tg_id}).mappings().first()
        user_id = int(user["id"])

    extra = await _ensure_trial_row(user_id)

    text_msg = start_text() + extra
    await (update.message or update.effective_message).reply_text(
        text_msg, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(tg_id)
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await (update.message or update.effective_message).reply_text(
        start_text(), parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(str(update.effective_user.id))
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with session_scope() as session:
        users = session.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
        premium = session.execute(text("SELECT COUNT(*) FROM users WHERE is_premium = TRUE")).scalar() or 0
        alerts = session.execute(text("SELECT COUNT(*) FROM alerts")).scalar() or 0
    msg = (
        "📊 <b>Bot Stats</b>\n\n"
        f"👥 Users: {users}\n"
        f"💎 Premium users: {premium}\n"
        f"🔔 Total alerts: {alerts}\n"
    )
    await (update.message or update.effective_message).reply_text(msg, parse_mode=ParseMode.HTML)

# -------------------- Lifecycle (start PTB inside FastAPI) --------------------
@app.on_event("startup")
async def on_startup():
    global tg_app
    init_db()

    tg_app = Application.builder().token(BOT_TOKEN).build()

    # register commands
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))
    tg_app.add_handler(CommandHandler("stats", cmd_stats))
    # admin
    register_admin_handlers(tg_app)

    # run bot in background task
    asyncio.create_task(tg_app.run_polling(allowed_updates=Update.ALL_TYPES))

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app and tg_app.running:
        await tg_app.stop()
    tg_app = None

# -------------------- Local dev entry --------------------
if __name__ == "__main__":
    # Local run: uvicorn server_combined:app --host 0.0.0.0 --port 10000
    uvicorn.run("server_combined:app", host="0.0.0.0", port=10000, reload=False)
