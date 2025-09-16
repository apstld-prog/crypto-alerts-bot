# server_combined.py
# Single process:
# - FastAPI health server
# - Telegram Bot (polling)
# - Alerts loop (background)
# - Extra features (Fear&Greed, Funding, Gainers/Losers, Chart, News, DCA, Pump alerts, Daily news)
# - Free plan (10 alerts) vs Premium (unlimited)
#
# Includes:
# - /start with all features sections
# - /listalts + /price fallback for off-Binance tokens (altcoins_info.py)
# - FIX: commit on deletions + handlers for ack:keep/ack:del
# - Admin commands implemented: /adminhelp /adminstats /adminsubs /admincheck /listalerts /testalert /resetalert /runalerts

from __future__ import annotations

import os
import re
import time
import threading
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
from db import init_db, session_scope, engine
from worker_logic import run_alert_cycle, resolve_symbol, fetch_price_binance

# ---- Extra features ----
from commands_extra import register_extra_handlers
from worker_extra import start_pump_watcher
from models_extras import init_extras

# ---- Plans ----
from plans import build_plan_info, can_create_alert, plan_status_line

# ---- Off-Binance info tokens ----
from altcoins_info import get_off_binance_info, list_off_binance

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
            print({"msg": "advisory_lock_acquired", "lock": name, "id": lock_id})
            return conn
        print({"msg": "advisory_lock_busy", "lock": name, "id": lock_id})
        conn.close()
        return None
    except Exception as e:
        print({"msg": "advisory_lock_error", "lock": name, "id": lock_id, "error": str(e)})
        return None

# ───────── Health server ─────────
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

@health_app.get("/billing/paypal/start")
def paypal_start(tg: str | None = Query(None), plan_id: str | None = Query(None)):
    plan = (plan_id or PAYPAL_PLAN_ID or "").strip()
    if PAYPAL_SUBSCRIBE_URL:
        target = PAYPAL_SUBSCRIBE_URL
    else:
        if not plan:
            return JSONResponse({"error": "No PAYPAL_SUBSCRIBE_URL and no plan_id available"}, status_code=400)
        target = f"https://www.paypal.com/webapps/billing/plans/subscribe?plan_id={plan}"
    try:
        parsed = urlparse(target)
        q = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if tg and "tg" not in q:
            q["tg"] = tg
        if plan and ("plan_id" not in q):
            q["plan_id"] = plan
        new_query = urlencode(q)
        new_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
        return RedirectResponse(new_url, status_code=302)
    except Exception as e:
        return PlainTextResponse(f"Redirect error: {e}", status_code=500)

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    def _run():
        uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="info")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print({"msg": "health_server_started", "port": port})

def bot_heartbeat_loop():
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

# ───────── UI helpers ─────────
def target_msg(update: Update):
    return update.message or (update.callback_query.message if update.callback_query else None)

def paypal_upgrade_url_for(tg_id: str | None) -> str | None:
    if WEB_URL and (PAYPAL_PLAN_ID or PAYPAL_SUBSCRIBE_URL) and tg_id:
        plan = PAYPAL_PLAN_ID or ""
        return f"{WEB_URL}/billing/paypal/start?tg={tg_id}" + (f"&plan_id={plan}" if plan else "")
    return PAYPAL_SUBSCRIBE_URL

def main_menu_keyboard(tg_id: str | None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Price BTC", callback_data="go:price:BTC"),
         InlineKeyboardButton("🔔 My Alerts", callback_data="go:myalerts")],
        [InlineKeyboardButton("⏱️ Set Alert Help", callback_data="go:setalerthelp"),
         InlineKeyboardButton("ℹ️ Help", callback_data="go:help")],
        [InlineKeyboardButton("🆘 Support", callback_data="go:support")]
    ]
    u = paypal_upgrade_url_for(tg_id)
    if u:
        rows.append([InlineKeyboardButton("💎 Upgrade with PayPal", url=u)])
    return InlineKeyboardMarkup(rows)

def upgrade_keyboard(tg_id: str | None):
    u = paypal_upgrade_url_for(tg_id)
    if u:
        return InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade with PayPal", url=u)]])
    return None

def start_text() -> str:
    return (
        "<b>Crypto Alerts Bot</b>\n"
        "⚡ Fast prices • 🧪 Diagnostics • 🔔 Alerts\n\n"
        "<b>Getting Started</b>\n"
        "• <code>/price BTC</code> — current price\n"
        "• <code>/setalert BTC &gt; 110000</code> — alert when condition is met\n"
        "• <code>/myalerts</code> — list your active alerts (with delete buttons)\n"
        "• <code>/help</code> — instructions\n"
        "• <code>/support &lt;message&gt;</code> — contact admin support\n\n"
        "💎 <b>Premium</b>: unlimited alerts\n"
        f"🆓 <b>Free</b>: up to <b>{FREE_ALERT_LIMIT}</b> alerts.\n\n"
        "<b>Extra Features</b>\n"
        "• <code>/feargreed</code> → current Fear &amp; Greed Index\n"
        "• <code>/funding [SYMBOL]</code> → futures funding rate or top extremes\n"
        "• <code>/topgainers</code>, <code>/toplosers</code> → 24h movers\n"
        "• <code>/chart &lt;SYMBOL&gt;</code> → mini chart (24h)\n"
        "• <code>/news [N]</code> → latest crypto headlines\n"
        "• <code>/dca &lt;amount_per_buy&gt; &lt;buys&gt; &lt;symbol&gt;</code>\n"
        "• <code>/pumplive on|off [threshold%]</code> → live pump alerts opt-in\n\n"
        "🌱 <b>New &amp; Off-Binance</b>\n"
        "• <code>/listalts</code> — curated off-Binance tokens (presales/community)\n"
        "• <code>/price HYPER</code> or <code>/price OZ</code> → info links if not on Binance yet\n\n"
        "🍀 <b>Supported</b>: most USDT pairs (BTC, ETH, SOL, XRP, ATOM, OSMO, INJ, DYDX, SEI, TIA, RUNE, KAVA, AKT, DOT, LINK, AVAX, MATIC, TON, SHIB, PEPE, …).\n"
    )

def safe_chunks(s: str, limit: int = 3800):
    while s:
        yield s[:limit]
        s = s[limit:]

def op_from_rule(rule: str) -> str:
    return ">" if rule == "price_above" else "<"

# ───────── Commands ─────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    _ = build_plan_info(tg_id, _ADMIN_IDS)  # ensure user row exists
    await target_msg(update).reply_text(
        start_text(),
        reply_markup=main_menu_keyboard(tg_id),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    help_html = (
        "<b>Help</b>\n\n"
        "• <code>/price &lt;SYMBOL&gt;</code> → Spot price\n"
        "• <code>/setalert &lt;SYMBOL&gt; &lt;op&gt; &lt;value&gt;</code>\n"
        "  Example: <code>/setalert BTC &gt; 110000</code>\n"
        "• <code>/myalerts</code> → list your active alerts\n"
        "• <code>/delalert &lt;id&gt;</code> → delete one alert (Premium only)\n"
        "• <code>/clearalerts</code> → delete ALL alerts (Premium only)\n"
        "• <code>/cancel_autorenew</code> → stop billing auto-renew\n"
        "• <code>/support &lt;message&gt;</code> → send message to admins\n"
        "• <code>/whoami</code> → shows your plan and role\n"
        "• <code>/requestcoin &lt;SYMBOL&gt;</code> → request new coin\n"
        "• <code>/adminhelp</code> → admin commands\n\n"
        "<b>Extra Features</b>\n"
        "• <code>/feargreed</code> • <code>/funding [SYMBOL]</code>\n"
        "• <code>/topgainers</code> • <code>/toplosers</code>\n"
        "• <code>/chart &lt;SYMBOL&gt;</code> • <code>/news [N]</code>\n"
        "• <code>/dca &lt;amount&gt; &lt;buys&gt; &lt;symbol&gt;</code>\n"
        "• <code>/pumplive on|off [threshold%]</code>\n"
        "• <code>/listalts</code> → curated off-Binance tokens\n"
    )
    for chunk in safe_chunks(help_html):
        await target_msg(update).reply_text(
            chunk,
            reply_markup=upgrade_keyboard(tg_id),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    await target_msg(update).reply_text(
        f"You are: {'admin' if plan.is_admin else 'user'}\nPremium: {plan.is_premium}\n{plan_status_line(plan)}"
    )

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = (context.args[0] if context.args else "BTC").upper()
    pair = resolve_symbol(symbol)

    if pair:
        price = fetch_price_binance(pair)
        if price is None:
            await target_msg(update).reply_text("Price fetch failed. Try again later.")
            return
        await target_msg(update).reply_text(f"{pair}: {price:.6f} USDT")
        return

    # Fallback: Off-Binance info coin
    info = get_off_binance_info(symbol)
    if info:
        lines = [f"ℹ️ <b>{info.get('name', symbol)}</b>\n{info.get('note','')}".strip()]
        for title, url in info.get("links", []):
            lines.append(f"• <a href=\"{url}\">{title}</a>")
        await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        return

    await target_msg(update).reply_text(
        "Unknown symbol for Binance price. Try BTC, ETH, SOL, ...\n"
        "Or see <code>/listalts</code> for off-Binance tokens.",
        parse_mode=ParseMode.HTML
    )

ALERT_RE = re.compile(r"^(?P<sym>[A-Za-z0-9/]+)\s*(?P<op>>|<)\s*(?P<val>[0-9]+(\.[0-9]+)?)$")

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await target_msg(update).reply_text("Usage: /setalert <SYMBOL> <op> <value>\nExample: /setalert BTC > 110000")
        return
    m = ALERT_RE.match(" ".join(context.args))
    if not m:
        await target_msg(update).reply_text("Format error. Example: /setalert BTC > 110000")
        return

    sym, op, val = m.group("sym"), m.group("op"), float(m.group("val"))
    pair = resolve_symbol(sym)
    if not pair:
        await target_msg(update).reply_text(
            "Unknown symbol. Try BTC, ETH, SOL, XRP, ATOM, OSMO, INJ, DYDX, SEI, TIA, RUNE, KAVA, AKT, DOT, LINK, AVAX, MATIC, TON, SHIB, PEPE ..."
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
            row = session.execute(text("""
                INSERT INTO alerts (user_id, symbol, rule, value, cooldown_seconds, user_seq, enabled)
                VALUES (
                    :uid, :sym, :rule, :val, :cooldown,
                    (SELECT COALESCE(MAX(user_seq), 0) + 1 FROM alerts WHERE user_id = :uid),
                    TRUE
                )
                RETURNING id, user_seq
            """), {"uid": plan.user_id, "sym": pair, "rule": rule, "val": val, "cooldown": 900}).first()
            user_seq = row.user_seq

        extra = "" if plan.has_unlimited else (f"  ({remaining-1} free slots left)" if remaining is not None and remaining > 0 else "")
        await target_msg(update).reply_text(f"✅ Alert A{user_seq} set: {pair} {op} {val}{extra}")
    except Exception as e:
        print({"msg": "setalert_error", "sym": sym, "pair": pair, "op": op, "val": val, "error": str(e)})
        await target_msg(update).reply_text(f"❌ Could not create alert: {e}")

def _alert_buttons(aid: int) -> InlineKeyboardMarkup:
    # Used in /myalerts list
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete", callback_data=f"del:{aid}")]])

async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    with session_scope() as session:
        rows = session.execute(text(
            "SELECT id, user_seq, symbol, rule, value, enabled "
            "FROM alerts WHERE user_id=:uid ORDER BY id DESC LIMIT 20"
        ), {"uid": plan.user_id}).all()
    if not rows:
        await target_msg(update).reply_text(f"No alerts in DB.\n{plan_status_line(plan)}")
        return
    for r in rows:
        op = op_from_rule(r.rule)
        txt = f"A{r.user_seq}  {r.symbol} {op} {r.value}  {'ON' if r.enabled else 'OFF'}"
        await target_msg(update).reply_text(txt, reply_markup=_alert_buttons(r.id))

async def cmd_delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    if not plan.has_unlimited:
        await target_msg(update).reply_text("This feature is for Premium users. Upgrade to delete alerts.")
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
        res = session.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"), {"id": aid, "uid": plan.user_id})
        session.commit()
        deleted = res.rowcount or 0
    await target_msg(update).reply_text("Alert (ID {0}) deleted.".format(aid) if deleted else "Nothing deleted. Check the id (or ownership).")

async def cmd_clearalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    if not plan.has_unlimited:
        await target_msg(update).reply_text("This feature is for Premium users. Upgrade to clear alerts.")
        return
    with session_scope() as session:
        res = session.execute(text("DELETE FROM alerts WHERE user_id=:uid"), {"uid": plan.user_id})
        session.commit()
        deleted = res.rowcount or 0
    await target_msg(update).reply_text(f"Deleted {deleted} alert(s).")

async def cmd_requestcoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await target_msg(update).reply_text("Usage: /requestcoin <SYMBOL>  e.g. /requestcoin ATOM")
        return
    sym = (context.args[0] or "").upper().strip()
    await target_msg(update).reply_text(f"Got it! We'll review and add {sym} if possible.")

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await target_msg(update).reply_text("Send: /support <your message to the admins>")
        return
    await target_msg(update).reply_text("✅ Your message has been sent to the support team.")

async def cmd_cancel_autorenew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEB_URL or not ADMIN_KEY:
        await target_msg(update).reply_text("Cancel not available right now. Try again later.")
        return
    tg_id = str(update.effective_user.id)
    try:
        r = requests.post(f"{WEB_URL}/billing/paypal/cancel", params={"telegram_id": tg_id, "key": ADMIN_KEY}, timeout=20)
        if r.status_code == 200:
            data = r.json()
            until = data.get("keeps_access_until")
            if until:
                await target_msg(update).reply_text(f"Auto-renew cancelled. Premium active until: {until}")
            else:
                await target_msg(update).reply_text("Auto-renew cancelled. Premium remains active till end of period.")
        else:
            await target_msg(update).reply_text(f"Cancel failed: {r.text}")
    except Exception as e:
        await target_msg(update).reply_text(f"Cancel error: {e}")

# --- Off-Binance list ---
async def cmd_listalts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = list_off_binance()
    if not syms:
        await target_msg(update).reply_text("No curated off-Binance tokens configured yet.")
        return
    lines = ["🌱 <b>Curated Off-Binance Tokens</b>"]
    for s in syms:
        info = get_off_binance_info(s)
        name = info.get("name", s) if info else s
        lines.append(f"• <code>{s}</code> — {name}")
    lines.append("\nTip: try <code>/price HYPER</code> or <code>/price OZ</code> for info links.")
    await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# ───────── Admin Commands ─────────
ADMIN_HELP = (
    "Admin Commands\n\n"
    "• /adminstats — users/premium/alerts/subs counters\n"
    "• /adminsubs — last 20 subscriptions\n"
    "• /admincheck — DB URL (masked) + last 5 alerts\n"
    "• /listalerts — last 20 alerts (id, rule, state)\n"
    "• /runalerts — run one alert-evaluation cycle now\n"
    "• /resetalert <id> — last_fired=NULL, last_met=FALSE\n"
    "• /testalert — quick DM test\n"
)

def _require_admin(update: Update) -> bool:
    tg_id = str(update.effective_user.id)
    return not is_admin(tg_id)

async def cmd_adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _require_admin(update):
        await target_msg(update).reply_text("Admins only.")
        return
    for chunk in safe_chunks(ADMIN_HELP):
        await target_msg(update).reply_text(chunk)

async def cmd_adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _require_admin(update):
        await target_msg(update).reply_text("Admins only.")
        return
    users_total = users_premium = alerts_total = alerts_active = 0
    subs_total = subs_active = subs_cancel_at_period_end = subs_cancelled = subs_unknown = 0
    subs_note = ""
    with session_scope() as session:
        try:
            users_total = session.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
            users_premium = session.execute(text("SELECT COUNT(*) FROM users WHERE is_premium = TRUE")).scalar_one()
        except Exception as e:
            subs_note += f"\n• users: {e}"
        try:
            alerts_total = session.execute(text("SELECT COUNT(*) FROM alerts")).scalar_one()
            alerts_active = session.execute(text("SELECT COUNT(*) FROM alerts WHERE enabled = TRUE")).scalar_one()
        except Exception as e:
            subs_note += f"\n• alerts: {e}"
        try:
            subs_total = session.execute(text("SELECT COUNT(*) FROM subscriptions")).scalar_one()
            subs_active = session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE status_internal = 'ACTIVE'")).scalar_one()
            subs_cancel_at_period_end = session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE status_internal = 'CANCEL_AT_PERIOD_END'")).scalar_one()
            subs_cancelled = session.execute(text("SELECT COUNT(*) FROM subscriptions WHERE status_internal = 'CANCELLED'")).scalar_one()
            subs_unknown = subs_total - subs_active - subs_cancel_at_period_end - subs_cancelled
        except Exception as e:
            subs_note += f"\n• subscriptions: {e}"
    msg = (
        "Admin Stats\n"
        f"Users: {users_total}  •  Premium: {users_premium}\n"
        f"Alerts: total={alerts_total}, active={alerts_active}\n"
        f"Subscriptions: total={subs_total}\n"
        f"  - ACTIVE={subs_active}\n"
        f"  - CANCEL_AT_PERIOD_END={subs_cancel_at_period_end}\n"
        f"  - CANCELLED={subs_cancelled}\n"
        f"  - UNKNOWN={subs_unknown}\n"
    )
    if subs_note:
        msg += "\nNotes:" + subs_note
    for chunk in safe_chunks(msg):
        await target_msg(update).reply_text(chunk)

async def cmd_adminsubs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _require_admin(update):
        await target_msg(update).reply_text("Admins only.")
        return
    with session_scope() as session:
        try:
            rows = session.execute(text("""
                SELECT s.id, s.user_id, s.provider, s.status_internal,
                       COALESCE(s.provider_sub_id,'') AS provider_ref,
                       s.created_at,
                       u.telegram_id
                FROM subscriptions s
                LEFT JOIN users u ON u.id = s.user_id
                ORDER BY s.id DESC
                LIMIT 20
            """)).all()
        except Exception as e:
            await target_msg(update).reply_text(f"subscriptions query error: {e}")
            return
    if not rows:
        await target_msg(update).reply_text("No subscriptions in DB.")
        return
    lines = []
    for r in rows:
        lines.append(f"#{r.id} uid={r.user_id or '-'} tg={r.telegram_id or '-'} "
                     f"{r.status_internal or '-'} ref={r.provider_ref or '-'} created={r.created_at.isoformat() if r.created_at else '-'}")
    msg = "Last 20 subscriptions:\n" + "\n".join(lines)
    for chunk in safe_chunks(msg):
        await target_msg(update).reply_text(chunk)

async def cmd_admincheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _require_admin(update):
        await target_msg(update).reply_text("Admins only.")
        return
    try:
        try:
            url_masked = engine.url.render_as_string(hide_password=True)
        except Exception:
            url_masked = str(engine.url)
        with session_scope() as session:
            total = session.execute(text("SELECT COUNT(*) FROM alerts")).scalar_one()
            rows = session.execute(text("""
                SELECT a.id, a.user_id, a.symbol, a.rule, a.value, a.enabled, u.telegram_id, a.last_fired_at, a.last_met
                FROM alerts a
                LEFT JOIN users u ON u.id = a.user_id
                ORDER BY a.id DESC
                LIMIT 5
            """)).all()
        lines = [f"DB: {url_masked}", f"alerts_total={total}", "last_5:"]
        if rows:
            for r in rows:
                op = op_from_rule(r.rule)
                lines.append(
                    f"  #{r.id} uid={r.user_id} tg={r.telegram_id or '-'} "
                    f"{r.symbol} {op} {r.value} {'ON' if r.enabled else 'OFF'} "
                    f"last_fired={r.last_fired_at} last_met={r.last_met}"
                )
        else:
            lines.append("  (none)")
        for chunk in safe_chunks("\n".join(lines)):
            await target_msg(update).reply_text(chunk)
    except Exception as e:
        await target_msg(update).reply_text(f"admincheck error: {e}")

async def cmd_listalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _require_admin(update):
        await target_msg(update).reply_text("Admins only.")
        return
    with session_scope() as session:
        rows = session.execute(text("""
            SELECT a.id, a.symbol, a.rule, a.value, a.enabled, a.last_fired_at, a.last_met
            FROM alerts a
            ORDER BY id DESC
            LIMIT 20
        """)).all()
    if not rows:
        await target_msg(update).reply_text("No alerts in DB.")
        return
    lines = []
    for r in rows:
        op = op_from_rule(r.rule)
        lines.append(
            f"#{r.id} {r.symbol} {op} {r.value} "
            f"{'ON' if r.enabled else 'OFF'} last_fired={r.last_fired_at or '-'} last_met={r.last_met}"
        )
    msg = "Last 20 alerts:\n" + "\n".join(lines)
    for chunk in safe_chunks(msg):
        await target_msg(update).reply_text(msg)

async def cmd_testalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    if _require_admin(update):
        await target_msg(update).reply_text("Admins only.")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": tg_id, "text": "Test alert ✅"}, timeout=10)
        await target_msg(update).reply_text(f"testalert status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        await target_msg(update).reply_text(f"testalert exception: {e}")

async def cmd_resetalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _require_admin(update):
        await target_msg(update).reply_text("Admins only.")
        return
    if not context.args:
        await target_msg(update).reply_text("Usage: /resetalert <id>")
        return
    try:
        aid = int(context.args[0])
    except Exception:
        await target_msg(update).reply_text("Bad id")
        return
    with session_scope() as session:
        row = session.execute(text("SELECT id FROM alerts WHERE id=:id"), {"id": aid}).first()
        if not row:
            await target_msg(update).reply_text(f"Alert {aid} not found")
            return
        session.execute(text("UPDATE alerts SET last_fired_at = NULL, last_met = FALSE WHERE id=:id"), {"id": aid})
        session.commit()
    await target_msg(update).reply_text(f"Alert (ID {aid}) reset (last_fired_at=NULL, last_met=FALSE).")

async def cmd_runalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _require_admin(update):
        await target_msg(update).reply_text("Admins only.")
        return
    with session_scope() as session:
        counters = run_alert_cycle(session)
        rows = session.execute(text("""
            SELECT id, user_id, symbol, rule, value, enabled, last_fired_at, last_met
            FROM alerts ORDER BY id DESC LIMIT 5
        """)).all()
    lines = [f"run_alert_cycle: {counters}", "last_5:"]
    for r in rows:
        op = op_from_rule(r.rule)
        lines.append(
            f"  #{r.id}  {r.symbol} {op} {r.value} "
            f"{'ON' if r.enabled else 'OFF'} last_fired={r.last_fired_at or '-'} last_met={r.last_met}"
        )
    for chunk in safe_chunks("\n".join(lines)):
        await target_msg(update).reply_text(chunk)

# ───────── Callbacks ─────────
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
        pair = resolve_symbol(sym)
        price = fetch_price_binance(pair) if pair else None
        if price is None:
            await query.message.reply_text("Price fetch failed. Try again later.")
        else:
            await query.message.reply_text(f"{pair}: {price:.6f} USDT")
        return
    if data == "go:setalerthelp":
        await query.message.reply_text("Examples:\n• /setalert BTC > 110000\n• /setalert ETH < 2000\nOps: >, < (USD number).")
        return
    if data == "go:support":
        await query.message.reply_text("Send a message to support:\n/support <your message>", reply_markup=upgrade_keyboard(tg_id))
        return

    # from /myalerts (delete one)
    if data.startswith("del:"):
        try:
            aid = int(data.split(":", 1)[1])
        except Exception:
            await query.edit_message_text("Bad id.")
            return
        with session_scope() as session:
            owner = session.execute(text("SELECT user_id FROM alerts WHERE id=:id"), {"id": aid}).first()
            if not owner:
                await query.edit_message_text("Alert not found.")
                return
            if owner.user_id != plan.user_id:
                await query.edit_message_text("You can delete only your own alerts.")
                return
            session.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"), {"id": aid, "uid": plan.user_id})
            session.commit()
        await query.edit_message_text("✅ Deleted alert.")
        return

    # inline buttons on triggered alert card: ack:keep:<id> / ack:del:<id>
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
                await query.answer("Kept 👍", show_alert=False)
            except Exception:
                await query.answer("Kept.")
            return

        if action == "del":
            with session_scope() as session:
                owner = session.execute(text("SELECT user_id FROM alerts WHERE id=:id"), {"id": aid}).first()
                if not owner:
                    await query.edit_message_text("Alert not found."); return
                if owner.user_id != plan.user_id:
                    await query.edit_message_text("You can delete only your own alerts."); return
                session.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
                                {"id": aid, "uid": plan.user_id})
                session.commit()
            try:
                await query.edit_message_text("✅ Alert deleted.")
            except Exception:
                await query.answer("Deleted.", show_alert=False)
            return

# ───────── Loops ─────────
def alerts_loop():
    global _ALERTS_LAST_OK_AT, _ALERTS_LAST_RESULT
    if not RUN_ALERTS:
        print({"msg": "alerts_disabled_env"})
        return

    lock_conn = acquire_advisory_lock(ALERTS_LOCK_ID, "alerts")
    if not lock_conn:
        print({"msg": "alerts_lock_skipped"})
        return

    print({"msg": "alerts_loop_start", "interval": INTERVAL_SECONDS})
    init_db()
    try:
        while True:
            ts = datetime.utcnow().isoformat()
            try:
                with session_scope() as session:
                    counters = run_alert_cycle(session)
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
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
        r = requests.get(url, timeout=10)
        print({"msg": "delete_webhook", "status": r.status_code, "body": r.text[:200]})
    except Exception as e:
        print({"msg": "delete_webhook_exception", "error": str(e)})

def run_bot():
    if not RUN_BOT:
        print({"msg": "bot_disabled_env"})
        return

    lock_conn = acquire_advisory_lock(BOT_LOCK_ID, "bot")
    if not lock_conn:
        print({"msg": "bot_lock_skipped"})
        return

    try:
        try:
            delete_webhook_if_any()
        except Exception:
            pass

        app = Application.builder().token(BOT_TOKEN).build()

        # Core commands
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("whoami", cmd_whoami))
        app.add_handler(CommandHandler("price", cmd_price))
        app.add_handler(CommandHandler("listalts", cmd_listalts))
        app.add_handler(CommandHandler("setalert", cmd_setalert))
        app.add_handler(CommandHandler("myalerts", cmd_myalerts))
        app.add_handler(CommandHandler("delalert", cmd_delalert))
        app.add_handler(CommandHandler("clearalerts", cmd_clearalerts))
        app.add_handler(CommandHandler("requestcoin", cmd_requestcoin))
        app.add_handler(CommandHandler("support", cmd_support))
        app.add_handler(CommandHandler("cancel_autorenew", cmd_cancel_autorenew))

        # Admin commands
        app.add_handler(CommandHandler("adminhelp", cmd_adminhelp))
        app.add_handler(CommandHandler("adminstats", cmd_adminstats))
        app.add_handler(CommandHandler("adminsubs", cmd_adminsubs))
        app.add_handler(CommandHandler("admincheck", cmd_admincheck))
        app.add_handler(CommandHandler("listalerts", cmd_listalerts))
        app.add_handler(CommandHandler("testalert", cmd_testalert))
        app.add_handler(CommandHandler("resetalert", cmd_resetalert))
        app.add_handler(CommandHandler("runalerts", cmd_runalerts))

        # Extra features
        register_extra_handlers(app)

        app.add_handler(CallbackQueryHandler(on_callback))
        print({"msg": "bot_start"})

        while True:
            try:
                app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
                break
            except Conflict as e:
                print({"msg": "bot_conflict_retry", "error": str(e)})
                time.sleep(5)
    finally:
        try:
            lock_conn.close()
        except Exception:
            pass

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
