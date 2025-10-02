# server_combined.py
from __future__ import annotations

import os
import re
import time
import threading
import html
from datetime import datetime, timedelta

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ApplicationHandlerStop
)

from sqlalchemy import text

# ───────────────────── Local modules ─────────────────────
from db import init_db, session_scope, engine
from worker_logic import run_alert_cycle, resolve_symbol, fetch_price_binance
from commands_extra import register_extra_handlers
from models_extras import init_extras
from altcoins_info import get_off_binance_info, list_off_binance, list_presales
from commands_plus import register_plus_handlers
from commands_advisor import register_advisor_handlers
from advisor_features import start_advisor_scheduler
from feedback_followup import start_feedback_scheduler

# ───────────────────── Configuration ─────────────────────
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
RUN_BOT = os.getenv("RUN_BOT", "1") == "1"
RUN_ALERTS = os.getenv("RUN_ALERTS", "1") == "1"

ADMIN_IDS = {s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()}

INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "10"))

BOT_LOCK_ID = int(os.getenv("BOT_LOCK_ID", "911001"))
ALERTS_LOCK_ID = int(os.getenv("ALERTS_LOCK_ID", "911002"))
_BOT_HEART_INTERVAL = int(os.getenv("BOT_HEART_INTERVAL_SECONDS", "60"))
_BOT_HEART_TTL = int(os.getenv("BOT_HEART_TTL_SECONDS", "180"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

# ───────────────────── Access DB schema ─────────────────────
def ensure_access_schema() -> None:
    with session_scope() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS user_access (
                user_id BIGINT PRIMARY KEY,
                trial_started_at TIMESTAMPTZ,
                premium_until TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """))
        s.execute(text("""
            CREATE OR REPLACE FUNCTION touch_user_access()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))
        s.execute(text("DROP TRIGGER IF EXISTS trg_touch_user_access ON user_access;"))
        s.execute(text("""
            CREATE TRIGGER trg_touch_user_access
            BEFORE UPDATE ON user_access
            FOR EACH ROW EXECUTE FUNCTION touch_user_access();
        """))
        s.commit()

def get_or_create_user_id(tg_id: str) -> int:
    with session_scope() as s:
        row = s.execute(text("SELECT id FROM users WHERE telegram_id=:t"), {"t": tg_id}).first()
        if row:
            return int(row.id)
        row = s.execute(text("INSERT INTO users (telegram_id) VALUES (:t) RETURNING id"), {"t": tg_id}).first()
        s.commit()
        return int(row.id)

def start_trial_if_needed(user_id: int) -> None:
    ensure_access_schema()
    with session_scope() as s:
        row = s.execute(text("SELECT trial_started_at FROM user_access WHERE user_id=:u"), {"u": user_id}).first()
        if row is None:
            s.execute(text("INSERT INTO user_access (user_id, trial_started_at) VALUES (:u, NOW())"), {"u": user_id})
        elif row.trial_started_at is None:
            s.execute(text("UPDATE user_access SET trial_started_at=NOW() WHERE user_id=:u"), {"u": user_id})
        s.commit()

def access_info(user_id: int) -> dict:
    ensure_access_schema()
    with session_scope() as s:
        row = s.execute(text("SELECT trial_started_at, premium_until FROM user_access WHERE user_id=:u"),
                        {"u": user_id}).first()
    return {
        "trial_started_at": (row.trial_started_at if row else None),
        "premium_until": (row.premium_until if row else None)
    }

def has_active_access(tg_id: str) -> tuple[bool, int]:
    """Return (active, days_left). Admins are always active."""
    if tg_id in ADMIN_IDS:
        return True, 9999
    uid = get_or_create_user_id(tg_id)
    ai = access_info(uid)
    now = datetime.utcnow()
    if ai["premium_until"] and ai["premium_until"] > now:
        days_left = max(0, (ai["premium_until"] - now).days)
        return True, days_left
    if ai["trial_started_at"]:
        left = (ai["trial_started_at"] + timedelta(days=TRIAL_DAYS)) - now
        if left.total_seconds() > 0:
            return True, max(0, left.days)
    return False, 0

def grant_premium_days(target_tg_id: str, days: int) -> str:
    uid = get_or_create_user_id(target_tg_id)
    ensure_access_schema()
    with session_scope() as s:
        row = s.execute(text("SELECT premium_until FROM user_access WHERE user_id=:u"), {"u": uid}).first()
        now = datetime.utcnow()
        if row is None:
            base = now
            s.execute(text("INSERT INTO user_access (user_id, premium_until) VALUES (:u, :ts)"),
                      {"u": uid, "ts": now + timedelta(days=days)})
        else:
            base = row.premium_until if row.premium_until and row.premium_until > now else now
            s.execute(text("UPDATE user_access SET premium_until=:ts WHERE user_id=:u"),
                      {"u": uid, "ts": base + timedelta(days=days)})
        s.commit()
    expire = (base + timedelta(days=days)).strftime("%Y-%m-%d")
    return f"Granted {days} day(s). New premium_until: {expire}"

def trial_days_left_str(tg_id: str) -> str:
    uid = get_or_create_user_id(tg_id)
    ai = access_info(uid)
    now = datetime.utcnow()
    if ai["premium_until"] and ai["premium_until"] > now:
        d = (ai["premium_until"] - now).days
        return f"Premium active — expires in ~{d} day(s)."
    if ai["trial_started_at"]:
        end = ai["trial_started_at"] + timedelta(days=TRIAL_DAYS)
        left = end - now
        if left.total_seconds() > 0:
            return f"Free trial: ends on {end.strftime('%Y-%m-%d')} (≈{left.days} day(s) left)."
        return f"Trial ended on {end.strftime('%Y-%m-%d')}."
    return f"Free trial: {TRIAL_DAYS} days from first /start."

def count_users_started() -> int:
    ensure_access_schema()
    with session_scope() as s:
        row = s.execute(text("SELECT COUNT(*) AS c FROM user_access WHERE trial_started_at IS NOT NULL")).first()
    return int(row.c or 0)

# ───────────────── Binance symbols cache ─────────────────
_BINANCE_SYMBOLS: dict[str, str] = {}
_BINANCE_LAST_FETCH = 0.0
_BINANCE_TTL = 3600

def _refresh_binance_symbols(force: bool = False):
    global _BINANCE_LAST_FETCH, _BINANCE_SYMBOLS
    now = time.time()
    if (not force) and (now - _BINANCE_LAST_FETCH < _BINANCE_TTL) and _BINANCE_SYMBOLS:
        return
    try:
        r = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=15)
        data = r.json()
        mapping = {}
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            base = s.get("baseAsset", "")
            quote = s.get("quoteAsset", "")
            symbol = s.get("symbol", "")
            if quote == "USDT" and base and symbol:
                mapping[base.upper()] = symbol.upper()
        if mapping:
            _BINANCE_SYMBOLS = mapping
            _BINANCE_LAST_FETCH = now
            print({"msg": "binance_symbols_loaded", "count": len(mapping)})
    except Exception as e:
        print({"msg": "binance_symbols_error", "error": str(e)})

def resolve_symbol_auto(symbol: str | None) -> str | None:
    if not symbol:
        return None
    symbol = symbol.upper().strip()
    pair = resolve_symbol(symbol)
    if pair:
        return pair
    _refresh_binance_symbols()
    pair = _BINANCE_SYMBOLS.get(symbol)
    if pair:
        return pair
    _refresh_binance_symbols(force=True)
    return _BINANCE_SYMBOLS.get(symbol)

# ───────────────────── UI helpers ─────────────────────
def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Price BTC", callback_data="go:price:BTC"),
         InlineKeyboardButton("🔔 My Alerts", callback_data="go:myalerts")],
        [InlineKeyboardButton("⏱️ Set Alert Help", callback_data="go:setalerthelp"),
         InlineKeyboardButton("ℹ️ Help", callback_data="go:help")],
        [InlineKeyboardButton("🆘 Contact Admin", callback_data="go:support")]
    ]
    return InlineKeyboardMarkup(rows)

def start_text(tg_id: str) -> str:
    return (
        "<b>Crypto Alerts Bot</b>\n"
        "⚡ Fast prices • 🧪 Diagnostics • 🔔 Alerts\n\n"
        "🆓 <b>Free Trial:</b> full access for "
        f"<b>{TRIAL_DAYS} days</b> from your first /start.\n"
        f"{html.escape(trial_days_left_str(tg_id))}\n\n"
        "<b>Highlights</b>\n"
        "• Prices: <code>/price BTC</code>, mini <code>/chart BTC</code>\n"
        "• Alerts: <code>/setalert BTC &gt; 110000</code>, <code>/myalerts</code>\n"
        "• Market: <code>/feargreed</code>, <code>/funding</code>, <code>/topgainers</code>, <code>/toplosers</code>, <code>/news</code>, <code>/dca</code>\n"
        "• Advisor: <code>/setadvisor</code>, <code>/myadvisor</code>, <code>/rebalance_now</code>\n"
        "• Alts/Presales: <code>/listalts</code>, <code>/listpresales</code>, <code>/alts &lt;SYMBOL&gt;</code>\n\n"
        "After the trial, ask the admin to extend access via <b>Contact Admin</b>."
    )

def safe_chunks(s: str, limit: int = 3800):
    while s:
        yield s[:limit]
        s = s[limit:]

def op_from_rule(rule: str) -> str:
    return ">" if rule == "price_above" else "<"

# ───────────────────── FastAPI health ─────────────────────
health_app = FastAPI()
_BOT_HEART_BEAT_AT = None
_BOT_HEART_STATUS = "unknown"
_ALERTS_LAST_OK_AT = None
_ALERTS_LAST_RESULT = None

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
    return {"bot": ("stale" if stale else _BOT_HEART_STATUS)}

@health_app.get("/alertsok")
def alertsok():
    return {"last_ok": _ALERTS_LAST_OK_AT.isoformat() + "Z" if _ALERTS_LAST_OK_AT else None,
            "last_result": _ALERTS_LAST_RESULT or {}}

def bot_heartbeat_loop():
    global _BOT_HEART_BEAT_AT, _BOT_HEART_STATUS
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    print({"msg": "bot_heartbeat_started", "interval": _BOT_HEART_INTERVAL})
    while True:
        try:
            r = requests.get(url, timeout=10)
            _BOT_HEART_STATUS = "ok" if (r.status_code == 200 and r.json().get("ok") is True) else "fail"
        except Exception:
            _BOT_HEART_STATUS = "fail"
        _BOT_HEART_BEAT_AT = datetime.utcnow()
        time.sleep(_BOT_HEART_INTERVAL)

# ───────────────────── Access guard (trial/premium) ─────────────────────
async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        tg_id = str(update.effective_user.id)

        # Admins always allowed
        if tg_id in ADMIN_IDS:
            return

        active, _days = has_active_access(tg_id)
        if active:
            return

        # Block and show how to contact admin
        info = trial_days_left_str(tg_id)
        text_msg = (
            "🚫 Your access is not active.\n"
            f"{info}\n\n"
            "Tap the button below to contact the admin and request an extension."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Contact Admin", callback_data="go:support")]])
        await msg.reply_text(text_msg, reply_markup=kb, disable_web_page_preview=True)
        raise ApplicationHandlerStop()
    except ApplicationHandlerStop:
        raise
    except Exception as e:
        print({"msg": "access_guard_error", "error": str(e)})

# ───────────────────── Commands ─────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    user_id = get_or_create_user_id(tg_id)
    start_trial_if_needed(user_id)
    await (update.message or update.callback_query.message).reply_text(
        start_text(tg_id),
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    help_html = (
        "<b>Help</b>\n\n"
        f"{html.escape(trial_days_left_str(tg_id))}\n\n"
        "• <code>/price &lt;SYMBOL&gt;</code> → Spot price (auto-detect Binance USDT listings)\n"
        "• <code>/setalert &lt;SYMBOL&gt; &lt;op&gt; &lt;value&gt;</code>  e.g. <code>/setalert BTC &gt; 110000</code>\n"
        "• <code>/myalerts</code>, <code>/delalert &lt;id&gt;</code>, <code>/clearalerts</code>\n"
        "• <code>/feargreed</code> • <code>/funding [SYMBOL]</code> • <code>/topgainers</code> • <code>/toplosers</code>\n"
        "• <code>/chart &lt;SYMBOL&gt;</code> • <code>/news [N]</code> • <code>/dca &lt;amount&gt; &lt;buys&gt; &lt;symbol&gt;</code>\n"
        "• <code>/pumplive on|off [threshold%]</code>\n"
        "• <code>/alts &lt;SYMBOL&gt;</code> • <code>/listalts</code> • <code>/listpresales</code>\n"
        "• <code>/setadvisor</code> • <code>/myadvisor</code> • <code>/rebalance_now</code>\n"
        "\n<i>After the trial, contact admin to extend access.</i>"
    )
    for chunk in safe_chunks(help_html):
        await (update.message or update.callback_query.message).reply_text(
            chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = (context.args[0] if context.args else "BTC").upper()
    pair = resolve_symbol_auto(symbol)
    if pair:
        price = fetch_price_binance(pair)
        if price is None:
            await update.effective_message.reply_text("Price fetch failed. Try again later.")
            return
        await update.effective_message.reply_text(f"{pair}: {price:.6f} USDT")
        return
    info = get_off_binance_info(symbol)
    if info:
        lines = [f"ℹ️ <b>{html.escape(info.get('name', symbol))}</b>\n{html.escape(info.get('note',''))}".strip()]
        for title, url in info.get("links", []):
            lines.append(f"• <a href=\"{html.escape(url)}\">{html.escape(title)}</a>")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    await update.effective_message.reply_text(
        "Unknown symbol. Try BTC, ETH, SOL… or <code>/alts SYMBOL</code>.",
        parse_mode=ParseMode.HTML,
    )

ALERT_RE = re.compile(r"^(?P<sym>[A-Za-z0-9/]+)\s*(?P<op>>|<)\s*(?P<val>[0-9]+(\.[0-9]+)?)$")

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setalert <SYMBOL> <op> <value>\nExample: /setalert BTC > 110000"
        )
        return
    m = ALERT_RE.match(" ".join(context.args))
    if not m:
        await update.effective_message.reply_text("Format error. Example: /setalert BTC > 110000")
        return
    sym, op, val = m.group("sym"), m.group("op"), float(m.group("val"))
    pair = resolve_symbol_auto(sym)
    if not pair:
        await update.effective_message.reply_text(
            "Unknown symbol. Try BTC, ETH, SOL … or <code>/alts SYMBOL</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    tg_id = str(update.effective_user.id)
    user_id = get_or_create_user_id(tg_id)

    rule = "price_above" if op == ">" else "price_below"
    try:
        with session_scope() as session:
            row = session.execute(
                text("""
                    INSERT INTO alerts (user_id, symbol, rule, value, cooldown_seconds, user_seq, enabled)
                    VALUES (:uid, :sym, :rule, :val, :cooldown,
                            (SELECT COALESCE(MAX(user_seq),0)+1 FROM alerts WHERE user_id=:uid),
                            TRUE)
                    RETURNING id, user_seq
                """),
                {"uid": user_id, "sym": pair, "rule": rule, "val": val, "cooldown": 900},
            ).first()
            user_seq = row.user_seq
        await update.effective_message.reply_text(f"✅ Alert A{user_seq} set: {pair} {op} {val}")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Could not create alert: {e}")

def _alert_buttons(aid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete", callback_data=f"del:{aid}")]])

async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    uid = get_or_create_user_id(tg_id)
    with session_scope() as session:
        rows = session.execute(
            text("SELECT id, user_seq, symbol, rule, value, enabled FROM alerts WHERE user_id=:uid ORDER BY id DESC LIMIT 20"),
            {"uid": uid},
        ).all()
    if not rows:
        await update.effective_message.reply_text("No alerts in DB.")
        return
    for r in rows:
        op = op_from_rule(r.rule)
        await update.effective_message.reply_text(
            f"A{r.user_seq}  {r.symbol} {op} {r.value}  {'ON' if r.enabled else 'OFF'}",
            reply_markup=_alert_buttons(r.id),
        )

async def cmd_delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    uid = get_or_create_user_id(tg_id)
    if not context.args:
        await update.effective_message.reply_text("Usage: /delalert <id>")
        return
    try:
        aid = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text("Bad id")
        return
    with session_scope() as session:
        res = session.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
                              {"id": aid, "uid": uid})
        session.commit()
    await update.effective_message.reply_text("Deleted." if (res.rowcount or 0) > 0 else "Nothing deleted (check id/ownership).")

async def cmd_clearalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    uid = get_or_create_user_id(tg_id)
    with session_scope() as session:
        session.execute(text("DELETE FROM alerts WHERE user_id=:uid"), {"uid": uid})
        session.commit()
    await update.effective_message.reply_text("All your alerts were deleted.")

# ───────────────────── Alts / Presales ─────────────────────
async def cmd_alts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage: /alts <SYMBOL>")
        return
    sym = (context.args[0] or "").upper().strip()
    info = get_off_binance_info(sym)
    if not info:
        await update.effective_message.reply_text("No curated info for that symbol.")
        return
    lines = [f"ℹ️ <b>{html.escape(info.get('name', sym))}</b>\n{html.escape(info.get('note',''))}".strip()]
    for title, url in info.get("links", []):
        lines.append(f"• <a href=\"{html.escape(url)}\">{html.escape(title)}</a>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_listalts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = list_off_binance()
    if not syms:
        await update.effective_message.reply_text("No curated tokens configured yet.")
        return
    lines = ["🌱 <b>Curated Off-Binance & Community</b>"] + [f"• <code>{html.escape(s)}</code>" for s in syms]
    lines.append("\nTip: /alts &lt;SYMBOL&gt; for notes & links.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_listpresales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = list_presales()
    if not syms:
        await update.effective_message.reply_text("No presales listed yet.")
        return
    lines = ["🟠 <b>Curated Presales</b>"] + [f"• <code>{html.escape(s)}</code>" for s in syms]
    lines.append("\nTip: /alts &lt;SYMBOL&gt; for notes & links. DYOR • High risk.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ───────────────────── Admin commands ─────────────────────
def _is_admin(update: Update) -> bool:
    return str(update.effective_user.id) in ADMIN_IDS

async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /grant <telegram_id> <days>")
        return
    target = context.args[0].strip()
    try:
        days = int(context.args[1])
    except Exception:
        await update.effective_message.reply_text("Bad <days> number.")
        return
    msg = grant_premium_days(target, days)
    await update.effective_message.reply_text(f"✅ {msg}")

async def cmd_userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    c = count_users_started()
    await update.effective_message.reply_text(f"👥 Users who pressed /start (trial started): {c}")

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    active, days = has_active_access(tg_id)
    await update.effective_message.reply_text(
        f"You are: {'admin' if _is_admin(update) else 'user'}\n"
        f"Access active: {active}\n"
        f"{trial_days_left_str(tg_id)}"
    )

# ───────────────────── Callbacks (with access check) ─────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Loading...", show_alert=False)
    data = (query.data or "").strip()

    # Access check for callbacks (non-admin)
    tg_id = str(query.from_user.id)
    if tg_id not in ADMIN_IDS:
        active, _days = has_active_access(tg_id)
        if not active and not data.startswith("go:support"):
            info = trial_days_left_str(tg_id)
            await query.message.reply_text(
                "🚫 Your access is not active.\n"
                f"{info}\n\nTap the button below to contact the admin.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🆘 Contact Admin", callback_data="go:support")]]
                ),
                disable_web_page_preview=True,
            )
            return

    if data == "go:help":
        await cmd_help(update, context); return
    if data == "go:myalerts":
        await cmd_myalerts(update, context); return
    if data.startswith("go:price:"):
        sym = data.split(":", 2)[2]
        pair = resolve_symbol_auto(sym)
        price = fetch_price_binance(pair) if pair else None
        await query.message.reply_text("Price fetch failed." if price is None else f"{pair}: {price:.6f} USDT")
        return
    if data == "go:setalerthelp":
        await query.message.reply_text("Examples:\n• /setalert BTC > 110000\n• /setalert ETH < 2000")
        return
    if data == "go:support":
        await query.message.reply_text("Send /support <message> and an admin will contact you.")
        return

    if data.startswith("del:"):
        try:
            aid = int(data.split(":", 1)[1])
        except Exception:
            await query.edit_message_text("Bad id."); return
        uid = get_or_create_user_id(tg_id)
        with session_scope() as s:
            owner = s.execute(text("SELECT user_id FROM alerts WHERE id=:id"), {"id": aid}).first()
            if not owner:
                await query.edit_message_text("Alert not found."); return
            if owner.user_id != uid:
                await query.edit_message_text("You can delete only your own alerts."); return
            s.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
                      {"id": aid, "uid": uid})
            s.commit()
        await query.edit_message_text("✅ Deleted alert.")
        return

# ───────────────────── Worker loops / run ─────────────────────
def alerts_loop():
    global _ALERTS_LAST_OK_AT, _ALERTS_LAST_RESULT
    if not RUN_ALERTS:
        print({"msg": "alerts_disabled_env"}); return
    lock_conn = engine.connect()
    got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": ALERTS_LOCK_ID}).scalar()
    if not got:
        print({"msg": "alerts_lock_skipped"}); lock_conn.close(); return
    print({"msg": "alerts_loop_start", "interval": INTERVAL_SECONDS}); init_db()
    try:
        while True:
            ts = datetime.utcnow().isoformat()
            try:
                with session_scope() as s:
                    counters = run_alert_cycle(s)
                _ALERTS_LAST_RESULT = {"ts": ts, **counters}
                _ALERTS_LAST_OK_AT = datetime.utcnow()
                print({"msg": "alert_cycle", **_ALERTS_LAST_RESULT})
            except Exception as e:
                print({"msg": "alert_cycle_error", "ts": ts, "error": str(e)})
            time.sleep(INTERVAL_SECONDS)
    finally:
        try: lock_conn.close()
        except Exception: pass

def delete_webhook_if_any():
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
        print({"msg": "delete_webhook", "status": r.status_code, "body": r.text[:160]})
    except Exception as e:
        print({"msg": "delete_webhook_exception", "error": str(e)})

def run_bot():
    if not RUN_BOT:
        print({"msg": "bot_disabled_env"}); return
    lock_conn = engine.connect()
    got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": BOT_LOCK_ID}).scalar()
    if not got:
        print({"msg": "bot_lock_skipped"}); lock_conn.close(); return
    try:
        try: delete_webhook_if_any()
        except Exception: pass

        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .read_timeout(40)
            .connect_timeout(15)
            .build()
        )

        # Access guard BEFORE all commands (except /start)
        app.add_handler(MessageHandler(filters.COMMAND & (~filters.Regex(r"^/start")), access_guard), group=-1)

        # Core & admin
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("whoami", cmd_whoami))
        app.add_handler(CommandHandler("userstats", cmd_userstats))  # admin
        app.add_handler(CommandHandler("grant", cmd_grant))          # admin

        # Price & alerts
        app.add_handler(CommandHandler("price", cmd_price))
        app.add_handler(CommandHandler("setalert", cmd_setalert))
        app.add_handler(CommandHandler("myalerts", cmd_myalerts))
        app.add_handler(CommandHandler("delalert", cmd_delalert))
        app.add_handler(CommandHandler("clearalerts", cmd_clearalerts))

        # Alts / presales
        app.add_handler(CommandHandler("alts", cmd_alts))
        app.add_handler(CommandHandler("listalts", cmd_listalts))
        app.add_handler(CommandHandler("listpresales", cmd_listpresales))

        # Extra feature packs
        register_extra_handlers(app)
        register_plus_handlers(app)
        register_advisor_handlers(app)

        # Callbacks
        app.add_handler(CallbackQueryHandler(on_callback))

        print({"msg": "bot_start"})
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    finally:
        try: lock_conn.close()
        except Exception: pass

def main():
    init_db()
    init_extras()
    ensure_access_schema()

    port = int(os.getenv("PORT", "10000"))
    threading.Thread(
        target=lambda: uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="info"),
        daemon=True
    ).start()
    threading.Thread(target=bot_heartbeat_loop, daemon=True).start()
    start_advisor_scheduler()
    start_feedback_scheduler()
    threading.Thread(target=alerts_loop, daemon=True).start()
    run_bot()

if __name__ == "__main__":
    main()
