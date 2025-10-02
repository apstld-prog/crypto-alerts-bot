# server_combined.py
from __future__ import annotations

import os
import re
import time
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, JSONResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Conflict, TimedOut as TgTimedOut
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ApplicationHandlerStop
)

from sqlalchemy import text

# Local modules (όπως ήταν)
from db import init_db, session_scope, engine
from worker_logic import run_alert_cycle, resolve_symbol, fetch_price_binance
from commands_extra import register_extra_handlers
from worker_extra import start_pump_watcher
from models_extras import init_extras
from plans import build_plan_info, can_create_alert, plan_status_line
from altcoins_info import get_off_binance_info, list_off_binance, list_presales
from commands_admin import register_admin_handlers  # Admin module (μένει)

# ─────────────────────────── ENV / CONFIG ───────────────────────────

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
WEB_URL = (os.getenv("WEB_URL") or "").strip() or None
ADMIN_KEY = (os.getenv("ADMIN_KEY") or "").strip() or None

INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))
FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT", "10"))

RUN_BOT = os.getenv("RUN_BOT", "1") == "1"
RUN_ALERTS = os.getenv("RUN_ALERTS", "1") == "1"

_ADMIN_IDS = {s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()}
BOT_LOCK_ID = int(os.getenv("BOT_LOCK_ID", "911001"))
ALERTS_LOCK_ID = int(os.getenv("ALERTS_LOCK_ID", "911002"))

_BOT_HEART_INTERVAL = int(os.getenv("BOT_HEART_INTERVAL_SECONDS", "60"))
_BOT_HEART_TTL = int(os.getenv("BOT_HEART_TTL_SECONDS", "180"))

TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "10"))  # 10ήμερο trial

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

# ─────────────── In-memory access store (trial/premium) ──────────────
# Δεν αλλάζουμε DB. Απλό, γρήγορο, και δεν επηρεάζει τα παλιά handlers.
_access: dict[int, dict[str, object]] = {}  # {tg_id: {"trial_end": datetime, "premium": bool}}

def _is_admin(tg_id: int) -> bool:
    return str(tg_id) in _ADMIN_IDS

def _get_access(tg_id: int) -> dict[str, object]:
    u = _access.get(tg_id)
    if not u:
        u = {"trial_end": datetime.utcnow() + timedelta(days=TRIAL_DAYS), "premium": False}
        _access[tg_id] = u
    return u

def _has_access(tg_id: int) -> bool:
    if _is_admin(tg_id):
        return True
    u = _get_access(tg_id)
    return u["premium"] or (datetime.utcnow() <= u["trial_end"])

def _trial_days_left(tg_id: int) -> int:
    u = _get_access(tg_id)
    return max(0, (u["trial_end"] - datetime.utcnow()).days)

# ───────────────── Binance symbols cache (auto-detect) ──────────────

_BINANCE_SYMBOLS: dict[str, str] = {}   # base → pair (e.g., BTC -> BTCUSDT)
_BINANCE_LAST_FETCH = 0.0
_BINANCE_TTL = int(os.getenv("BINANCE_EXCHANGEINFO_TTL", "3600"))  # seconds

def _refresh_binance_symbols(force: bool = False):
    """Refresh Binance USDT trading pairs and keep a base->symbol map."""
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
    """Try current mapping, otherwise ask Binance (cached) for new listings."""
    if not symbol:
        return None
    symbol = symbol.upper().strip()
    # 1) your static mapping (worker_logic.resolve_symbol)
    pair = resolve_symbol(symbol)
    if pair:
        return pair
    # 2) cached Binance listing
    _refresh_binance_symbols()
    pair = _BINANCE_SYMBOLS.get(symbol)
    if pair:
        return pair
    # 3) force refresh once
    _refresh_binance_symbols(force=True)
    return _BINANCE_SYMBOLS.get(symbol)

# ───────────────────────── Small helpers ─────────────────────────────

def target_msg(update: Update):
    """Return a message target compatible with commands & callbacks."""
    return update.message or (update.callback_query.message if update.callback_query else None)

def main_menu_keyboard(tg_id: str | None) -> InlineKeyboardMarkup:
    # Χωρίς PayPal κουμπί
    rows = [
        [InlineKeyboardButton("📊 Price BTC", callback_data="go:price:BTC"),
         InlineKeyboardButton("🔔 My Alerts", callback_data="go:myalerts")],
        [InlineKeyboardButton("⏱️ Set Alert Help", callback_data="go:setalerthelp"),
         InlineKeyboardButton("ℹ️ Help", callback_data="go:help")],
        [InlineKeyboardButton("🆘 Support", callback_data="go:support")]
    ]
    return InlineKeyboardMarkup(rows)

def start_text(tg_id: int, first_name: str | None) -> str:
    days_left = _trial_days_left(tg_id)
    trial_end = _get_access(tg_id)["trial_end"]
    return (
        "<b>Crypto Alerts Bot</b>\n"
        "⚡ Fast prices • 🧪 Diagnostics • 🔔 Alerts\n\n"
        f"👋 Welcome { (first_name or 'friend').upper() }!\n"
        f"✅ You have FULL access for <b>{TRIAL_DAYS}</b> days.\n"
        f"📅 Trial ends: <b>{trial_end.strftime('%Y-%m-%d %H:%M UTC')}</b>\n"
        f"⏳ Days left: <b>{days_left}</b>\n\n"
        "<b>Getting Started</b>\n"
        "• <code>/price BTC</code> — current price\n"
        "• <code>/setalert BTC &gt; 110000</code> — alert when condition is met\n"
        "• <code>/myalerts</code> — list your active alerts (with delete buttons)\n"
        "• <code>/help</code> — instructions\n"
        "• <code>/support &lt;message&gt;</code> — contact admin support\n\n"
        "💎 <b>Premium</b>: unlimited alerts\n"
        f"🆓 <b>Free</b>: up to <b>{FREE_ALERT_LIMIT}</b> alerts.\n\n"
        "<b>Extra Features</b>\n"
        "• <code>/feargreed</code> • <code>/funding [SYMBOL]</code>\n"
        "• <code>/topgainers</code> • <code>/toplosers</code>\n"
        "• <code>/chart &lt;SYMBOL&gt;</code> • <code>/news [N]</code>\n"
        "• <code>/dca &lt;amount_per_buy&gt; &lt;buys&gt; &lt;symbol&gt;</code>\n"
        "• <code>/pumplive on|off [threshold%]</code>\n"
        "• <code>/listalts</code>, <code>/listpresales</code>, <code>/alts &lt;SYMBOL&gt;</code>\n\n"
        "🌱 <b>New &amp; Off-Binance</b> — Try <code>/alts HYPER</code> or <code>/alts OZ</code> for info.\n"
        "If a token gets listed on Binance later, <code>/price</code> will auto-detect it.\n"
    )

def safe_chunks(s: str, limit: int = 3800):
    while s:
        yield s[:limit]
        s = s[limit:]

def op_from_rule(rule: str) -> str:
    return ">" if rule == "price_above" else "<"

# ─────────────────────────── FastAPI Health ─────────────────────────

health_app = FastAPI()
_BOT_HEART_BEAT_AT = None
_BOT_HEART_STATUS = "unknown"
_ALERTS_LAST_OK_AT = None
_ALERTS_LAST_RESULT = None

@health_app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"ok": True, "service": "crypto-alerts-server"}

@health_app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}

@health_app.api_route("/botok", methods=["GET", "HEAD"])
def botok():
    now = datetime.utcnow()
    stale = (_BOT_HEART_BEAT_AT is None) or ((now - _BOT_HEART_BEAT_AT) > timedelta(seconds=_BOT_HEART_TTL))
    return {
        "bot": ("stale" if stale else _BOT_HEART_STATUS),
        "last": (_BOT_HEART_BEAT_AT.isoformat() + "Z") if _BOT_HEART_BEAT_AT else None,
        "ttl_seconds": _BOT_HEART_TTL,
        "interval_seconds": _BOT_HEART_INTERVAL,
    }

@health_app.api_route("/alertsok", methods=["GET", "HEAD"])
def alertsok():
    return {
        "last_ok": (_ALERTS_LAST_OK_AT.isoformat() + "Z") if _ALERTS_LAST_OK_AT else None,
        "last_result": _ALERTS_LAST_RESULT or {},
        "expected_interval_seconds": INTERVAL_SECONDS,
    }

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

# ─────────────────────────── Access Guard ───────────────────────────
ALWAYS_ALLOWED = {
    "start", "help", "whoami", "support",
    # admin tools (δικά μας)
    "extend", "setpremium", "listusers", "stats"
}

async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Κόβει όλες τις εντολές όταν έχει λήξει το trial και δεν είναι premium/admin."""
    msg = update.effective_message
    if not msg or not msg.text or not msg.text.startswith("/"):
        return
    tg_id = update.effective_user.id if update.effective_user else 0
    cmd = msg.text.split()[0].split("@")[0][1:].lower()

    if cmd in ALWAYS_ALLOWED or _is_admin(tg_id) or _has_access(tg_id):
        return

    await msg.reply_text("⛔ Το δοκιμαστικό σου έληξε. Στείλε /support για επέκταση από admin.")
    raise ApplicationHandlerStop

# ─────────────────────────── Bot Commands ──────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    _get_access(tg_id)  # ensure trial exists
    _ = build_plan_info(str(tg_id), _ADMIN_IDS)  # ensure user exists στη δική σου DB/λογική
    await target_msg(update).reply_text(
        start_text(tg_id, update.effective_user.first_name),
        reply_markup=main_menu_keyboard(str(tg_id)),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    help_html = (
        "<b>Help</b>\n\n"
        "• <code>/price &lt;SYMBOL&gt;</code> → Spot price (auto-detects new Binance USDT listings)\n"
        "• <code>/setalert &lt;SYMBOL&gt; &lt;op&gt; &lt;value&gt;</code>  e.g. <code>/setalert BTC &gt; 110000</code>\n"
        "• <code>/myalerts</code> → list your alerts\n"
        "• <code>/delalert &lt;id&gt;</code>, <code>/clearalerts</code> → Premium\n"
        "• <code>/whoami</code> → plan info\n"
        "• <code>/support &lt;message&gt;</code> → contact admins\n\n"
        "<b>Market Tools</b>\n"
        "• <code>/feargreed</code> • <code>/funding [SYMBOL]</code> • <code>/topgainers</code> • <code>/toplosers</code>\n"
        "• <code>/chart &lt;SYMBOL&gt;</code> • <code>/news [N]</code> • <code>/dca &lt;amount&gt; &lt;buys&gt; &lt;symbol&gt;</code>\n"
        "• <code>/pumplive on|off [threshold%]</code>\n\n"
        "<b>Alts / Presales</b>\n"
        "• <code>/alts &lt;SYMBOL&gt;</code> → notes &amp; links only\n"
        "• <code>/listalts</code> → curated off-Binance/community\n"
        "• <code>/listpresales</code> → curated presales (very high risk)\n"
    )
    for chunk in safe_chunks(help_html):
        await target_msg(update).reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan = build_plan_info(str(update.effective_user.id), _ADMIN_IDS)
    u = _get_access(update.effective_user.id)
    await target_msg(update).reply_text(
        f"You are: {'admin' if plan.is_admin else 'user'}\n"
        f"Premium: {u['premium']} (plan: {plan.is_premium})\n"
        f"Trial end: {u['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"{plan_status_line(plan)}"
    )

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await target_msg(update).reply_text("Usage: /support <your message>")
        return
    msg = " ".join(context.args)
    await target_msg(update).reply_text("✅ Your message was sent to admin.")
    for admin in _ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=int(admin), text=f"[Support] from {update.effective_user.id}: {msg}")
        except Exception:
            pass

# ───────────────────────── Admin helper commands ────────────────────
async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if not _access:
        await target_msg(update).reply_text("📭 No users yet.")
        return
    lines = ["👥 Users (in-memory trial/premium):"]
    for uid, data in _access.items():
        lines.append(
            f"• {uid} | premium={data['premium']} | trial_end={data['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}"
        )
    await target_msg(update).reply_text("\n".join(lines))

async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await target_msg(update).reply_text("Usage: /extend <days> <user_id>")
        return
    try:
        days = int(context.args[0]); uid = int(context.args[1])
    except Exception as e:
        await target_msg(update).reply_text(f"Usage: /extend <days> <user_id>\nError: {e}")
        return
    u = _get_access(uid)
    u["trial_end"] = u["trial_end"] + timedelta(days=days)
    await target_msg(update).reply_text(
        f"✅ Extended {uid} by {days} day(s). New end: {u['trial_end'].strftime('%Y-%m-%d %H:%M UTC')}"
    )

async def cmd_setpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await target_msg(update).reply_text("Usage: /setpremium <user_id>")
        return
    try:
        uid = int(context.args[0])
    except Exception as e:
        await target_msg(update).reply_text(f"Usage: /setpremium <user_id>\nError: {e}")
        return
    u = _get_access(uid); u["premium"] = True
    await target_msg(update).reply_text(f"👑 {uid} is now PREMIUM (in-memory flag).")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    total = len(_access)
    premiums = sum(1 for v in _access.values() if v["premium"])
    await target_msg(update).reply_text(f"📊 Users: {total}\n⭐ Premium: {premiums}")

# ─────────────────────────── Price / Alerts (όπως ήταν) ─────────────

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = (context.args[0] if context.args else "BTC").upper()
    pair = resolve_symbol_auto(symbol)
    if pair:
        price = fetch_price_binance(pair)
        if price is None:
            await target_msg(update).reply_text("Price fetch failed. Try again later.")
            return
        await target_msg(update).reply_text(f"{pair}: {price:.6f} USDT")
        return
    info = get_off_binance_info(symbol)
    if info:
        lines = [f"ℹ️ <b>{info.get('name', symbol)}</b>\n{info.get('note','')}".strip()]
        for title, url in info.get("links", []):
            lines.append(f"• <a href=\"{url}\">{title}</a>")
        await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return
    await target_msg(update).reply_text(
        "Unknown symbol. Try BTC, ETH, SOL… or <code>/alts SYMBOL</code>.",
        parse_mode=ParseMode.HTML,
    )

ALERT_RE = re.compile(r"^(?P<sym>[A-Za-z0-9/]+)\s*(?P<op>>|<)\s*(?P<val>[0-9]+(\.[0-9]+)?)$")

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await target_msg(update).reply_text(
            "Usage: /setalert <SYMBOL> <op> <value>\nExample: /setalert BTC > 110000"
        )
        return
    m = ALERT_RE.match(" ".join(context.args))
    if not m:
        await target_msg(update).reply_text("Format error. Example: /setalert BTC > 110000")
        return
    sym, op, val = m.group("sym"), m.group("op"), float(m.group("val"))
    pair = resolve_symbol_auto(sym)
    if not pair:
        await target_msg(update).reply_text(
            "Unknown symbol. Try BTC, ETH, SOL … or <code>/alts SYMBOL</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    allowed, denial, remaining = can_create_alert(plan)
    if not allowed:
        await target_msg(update).reply_text(denial)
        return
    rule = "price_above" if op == ">" else "price_below"
    try:
        with session_scope() as session:
            row = session.execute(
                text(
                    """
                INSERT INTO alerts (user_id, symbol, rule, value, cooldown_seconds, user_seq, enabled)
                VALUES (:uid, :sym, :rule, :val, :cooldown,
                        (SELECT COALESCE(MAX(user_seq),0)+1 FROM alerts WHERE user_id=:uid),
                        TRUE)
                RETURNING id, user_seq
                """
                ),
                {"uid": plan.user_id, "sym": pair, "rule": rule, "val": val, "cooldown": 900},
            ).first()
            user_seq = row.user_seq
        extra = "" if plan.has_unlimited else (
            f"  ({(remaining-1) if remaining else 0} free slots left)" if remaining else ""
        )
        await target_msg(update).reply_text(f"✅ Alert A{user_seq} set: {pair} {op} {val}{extra}")
    except Exception as e:
        await target_msg(update).reply_text(f"❌ Could not create alert: {e}")

def _alert_buttons(aid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete", callback_data=f"del:{aid}")]])

async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT id, user_seq, symbol, rule, value, enabled FROM alerts "
                "WHERE user_id=:uid ORDER BY id DESC LIMIT 20"
            ),
            {"uid": plan.user_id},
        ).all()
    if not rows:
        await target_msg(update).reply_text(f"No alerts in DB.\n{plan_status_line(plan)}")
        return
    for r in rows:
        op = op_from_rule(r.rule)
        await target_msg(update).reply_text(
            f"A{r.user_seq}  {r.symbol} {op} {r.value}  {'ON' if r.enabled else 'OFF'}",
            reply_markup=_alert_buttons(r.id),
        )

async def cmd_delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan = build_plan_info(str(update.effective_user.id), _ADMIN_IDS)
    if not plan.has_unlimited:
        await target_msg(update).reply_text("This feature is for Premium users. Contact admin to upgrade.")
        return
    if not context.args:
        await target_msg(update).reply_text("Usage: /delalert <id>")
        return
    try:
        aid = int(context.args[0])
    except Exception:
        await target_msg(update).reply_text("Bad id")
        return
    with session_scope() as session:
        res = session.execute(
            text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
            {"id": aid, "uid": plan.user_id},
        )
        session.commit()
    await target_msg(update).reply_text("Deleted." if (res.rowcount or 0) > 0 else "Nothing deleted (check id/ownership).")

async def cmd_clearalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan = build_plan_info(str(update.effective_user.id), _ADMIN_IDS)
    if not plan.has_unlimited:
        await target_msg(update).reply_text("This feature is for Premium users. Contact admin to upgrade.")
        return
    with session_scope() as session:
        session.execute(text("DELETE FROM alerts WHERE user_id=:uid"), {"uid": plan.user_id})
        session.commit()
    await target_msg(update).reply_text("All your alerts were deleted.")

# ─────────────────────────── Alts / Presales ───────────────────────

async def cmd_alts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await target_msg(update).reply_text("Usage: /alts &lt;SYMBOL&gt;", parse_mode=ParseMode.HTML)
            return
        sym = (context.args[0] or "").upper().strip()
        info = get_off_binance_info(sym)
        if not info:
            await target_msg(update).reply_text("No curated info for that symbol.")
            return
        lines = [f"ℹ️ <b>{info.get('name', sym)}</b>\n{info.get('note','')}".strip()]
        for title, url in info.get("links", []):
            lines.append(f"• <a href=\"{url}\">{title}</a>")
        await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await target_msg(update).reply_text(f"Error: {e}")

async def cmd_listalts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        syms = list_off_binance()
        if not syms:
            await target_msg(update).reply_text("No curated tokens configured yet.")
            return
        lines = ["🌱 <b>Curated Off-Binance & Community</b>"]
        lines += [f"• <code>{s}</code>" for s in syms]
        lines.append("\nTip: /alts &lt;SYMBOL&gt; for notes &amp; links.")
        await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await target_msg(update).reply_text(f"Error: {e}")

async def cmd_listpresales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        syms = list_presales()
        if not syms:
            await target_msg(update).reply_text("No presales listed yet.")
            return
        lines = ["🟠 <b>Curated Presales</b>"]
        lines += [f"• <code>{s}</code>" for s in syms]
        lines.append("\nTip: /alts &lt;SYMBOL&gt; for notes &amp; links. DYOR • High risk.")
        await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await target_msg(update).reply_text(f"Error: {e}")

# ────────────────────── Callback buttons (inline) ───────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Loading...", show_alert=False)
    data = (query.data or "").strip()
    tg_id = str(query.from_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)

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
        await query.message.reply_text("Send /support <message>")
        return

    if data.startswith("del:"):
        try:
            aid = int(data.split(":", 1)[1])
        except Exception:
            await query.edit_message_text("Bad id."); return
        with session_scope() as s:
            owner = s.execute(text("SELECT user_id FROM alerts WHERE id=:id"), {"id": aid}).first()
            if not owner:
                await query.edit_message_text("Alert not found."); return
            if owner.user_id != plan.user_id:
                await query.edit_message_text("You can delete only your own alerts."); return
            s.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
                      {"id": aid, "uid": plan.user_id})
            s.commit()
        await query.edit_message_text("✅ Deleted alert.")
        return

    if data.startswith("ack:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Bad callback."); return
        action, aid_str = parts[1], parts[2]
        try:
            aid = int(aid_str)
        except Exception:
            await query.answer("Bad id."); return
        if action == "keep":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.answer("Kept 👍")
            except Exception:
                await query.answer("Kept.")
            return
        if action == "del":
            with session_scope() as s:
                owner = s.execute(text("SELECT user_id FROM alerts WHERE id=:id"), {"id": aid}).first()
                if not owner:
                    await query.edit_message_text("Alert not found."); return
                if owner.user_id != plan.user_id:
                    await query.edit_message_text("You can delete only your own alerts."); return
                s.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
                          {"id": aid, "uid": plan.user_id})
                s.commit()
            try:
                await query.edit_message_text("✅ Alert deleted.")
            except Exception:
                await query.answer("Deleted.")
            return

# ─────────────────────────── Worker loop ───────────────────────────

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
        try:
            lock_conn.close()
        except Exception:
            pass

def delete_webhook_if_any():
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
        print({"msg": "delete_webhook", "status": r.status_code, "body": r.text[:160]})
    except Exception as e:
        print({"msg": "delete_webhook_exception", "error": str(e)})

# ─────────────────────────── Run bot (polling) ─────────────────────

def run_bot():
    if not RUN_BOT:
        print({"msg": "bot_disabled_env"}); return

    lock_conn = engine.connect()
    got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": BOT_LOCK_ID}).scalar()
    if not got:
        print({"msg": "bot_lock_skipped"}); lock_conn.close(); return
    try:
        try:
            delete_webhook_if_any()
        except Exception:
            pass

        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .read_timeout(40)
            .connect_timeout(15)
            .build()
        )

        # 0) Global access guard (μπαίνει ΠΡΙΝ από όλα)
        app.add_handler(MessageHandler(filters.COMMAND, access_guard), group=0)

        # 1) Core commands
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("whoami", cmd_whoami))
        app.add_handler(CommandHandler("support", cmd_support))

        # 2) Admin helpers (in-memory trial/premium)
        app.add_handler(CommandHandler("listusers", cmd_listusers))
        app.add_handler(CommandHandler("extend", cmd_extend))
        app.add_handler(CommandHandler("setpremium", cmd_setpremium))
        app.add_handler(CommandHandler("stats", cmd_stats))

        # 3) Legacy commands (όπως τα είχες)
        app.add_handler(CommandHandler("price", cmd_price))
        app.add_handler(CommandHandler("alts", cmd_alts))
        app.add_handler(CommandHandler("listalts", cmd_listalts))
        app.add_handler(CommandHandler("listpresales", cmd_listpresales))
        app.add_handler(CommandHandler("setalert", cmd_setalert))
        app.add_handler(CommandHandler("myalerts", cmd_myalerts))
        app.add_handler(CommandHandler("delalert", cmd_delalert))
        app.add_handler(CommandHandler("clearalerts", cmd_clearalerts))

        # 4) Extras (funding/topgainers/chart/news/dca/pumplive κ.λπ.)
        register_extra_handlers(app)

        # 5) Admin module σου (μένει)
        register_admin_handlers(app, _ADMIN_IDS)

        # 6) Callback queries (inline buttons)
        app.add_handler(CallbackQueryHandler(on_callback))

        print({"msg": "bot_start"})

        backoff = 5
        while True:
            try:
                app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
                break
            except Conflict as e:
                print({"msg": "bot_conflict_retry", "error": str(e)})
                time.sleep(5)
            except TgTimedOut as e:
                print({"msg": "bot_timeout_retry", "error": str(e), "sleep": backoff})
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                print({"msg": "bot_generic_retry", "error": str(e), "sleep": 10})
                time.sleep(10)
    finally:
        try:
            lock_conn.close()
        except Exception:
            pass

# ─────────────────────────── Entry point ───────────────────────────

def main():
    init_db()
    init_extras()

    # Health server & heartbeat
    port = int(os.getenv("PORT", "10000"))
    threading.Thread(
        target=lambda: uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="info"),
        daemon=True
    ).start()
    threading.Thread(target=bot_heartbeat_loop, daemon=True).start()

    # Alerts worker
    threading.Thread(target=alerts_loop, daemon=True).start()

    # Pump watcher (extra)
    start_pump_watcher()

    # Bot (polling)
    run_bot()

if __name__ == "__main__":
    main()
