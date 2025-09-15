# server_combined.py
# Single process:
# - FastAPI health server
# - Telegram Bot (polling)
# - Alerts loop (background)
# - Extra features (Fear&Greed, Funding, Gainers/Losers, Chart, News, DCA, Pump alerts)
# - Free plan (10 alerts) vs Premium (unlimited), via plans.py
#
# Notes:
# - /whale command is disabled in commands_extra.py and removed from /help text.
# - /start message now includes a "Futures tools" section so users see futures capabilities.

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

def start_text(limit: int) -> str:
    # Start message includes Futures tools so users see the capability.
    return (
        "<b>Crypto Alerts Bot</b>\n"
        "⚡️ <i>Fast prices</i> • 🧪 <i>Diagnostics</i> • 🔔 <i>Alerts</i>\n\n"
        "<b>Plans</b>\n"
        f"🆓 <b>Free</b>: up to <b>{limit}</b> active alerts\n"
        "💎 <b>Premium</b>: unlimited alerts\n\n"
        "<b>Core</b>\n"
        "• <code>/price BTC</code> — current price\n"
        "• <code>/setalert BTC &gt; 110000</code> — alert when condition is met\n"
        "• <code>/myalerts</code> — manage your alerts\n"
        "• <code>/help</code> — all commands\n\n"
        "<b>Futures tools</b>\n"
        "• <code>/funding [SYMBOL]</code> — latest funding rate (e.g. <code>/funding BTC</code>)\n"
        "• <code>/pumplive on|off [threshold%]</code> — live pump alerts opt-in (default 10%)\n\n"
        "<b>More tools</b>\n"
        "• <code>/feargreed</code> — Fear &amp; Greed Index\n"
        "• <code>/topgainers</code>, <code>/toplosers</code> — 24h movers\n"
        "• <code>/chart &lt;SYMBOL&gt;</code> — mini 24h chart\n"
        "• <code>/news [N]</code> — latest crypto headlines\n"
        "• <code>/dca &lt;amount&gt; &lt;buys&gt; &lt;symbol&gt;</code>\n"
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
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    user_limit = 9999 if plan.has_unlimited else plan.free_limit
    await target_msg(update).reply_text(
        start_text(user_limit),
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
        "• <code>/feargreed</code> → Fear &amp; Greed Index\n"
        "• <code>/funding [SYMBOL]</code> → futures funding rate\n"
        "• <code>/topgainers</code>, <code>/toplosers</code>\n"
        "• <code>/chart &lt;SYMBOL&gt;</code> → mini 24h chart\n"
        "• <code>/news [N]</code> → latest crypto headlines\n"
        "• <code>/dca &lt;amount&gt; &lt;buys&gt; &lt;symbol&gt;</code>\n"
        "• <code>/pumplive on|off [threshold%]</code> → live pump alerts\n"
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
    if not pair:
        await target_msg(update).reply_text(
            "Unknown symbol. Try BTC, ETH, SOL, XRP, ATOM, OSMO, INJ, DYDX, SEI, TIA, RUNE, KAVA, AKT, DOT, LINK, AVAX, MATIC, TON, SHIB, PEPE ..."
        )
        return
    price = fetch_price_binance(pair)
    if price is None:
        await target_msg(update).reply_text("Price fetch failed. Try again later.")
        return
    await target_msg(update).reply_text(f"{pair}: {price:.6f} USDT")

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

def _alert_buttons(aid: int, seq: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑️ Delete A{seq}", callback_data=f"del:{aid}")]])

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
        await target_msg(update).reply_text(txt, reply_markup=_alert_buttons(r.id, r.user_seq))

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

ADMIN_HELP = (
    "Admin Commands\n\n"
    "• /adminstats — users/premium/alerts/subs counters\n"
    "• /adminsubs — last 20 subscriptions\n"
    "• /admincheck — DB URL (masked), last 5 alerts with last_fired/last_met\n"
    "• /listalerts — last 20 alerts (id, rule, state)\n"
    "• /runalerts — run one alert-evaluation cycle now\n"
    "• /resetalert <id> — last_fired=NULL, last_met=FALSE\n"
    "• /forcealert <id> — send alert now & set last_met=TRUE\n"
    "• /testalert — quick DM test\n"
)

async def cmd_adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    if not is_admin(tg_id):
        await target_msg(update).reply_text("Admins only.")
        return
    for chunk in safe_chunks(ADMIN_HELP):
        await target_msg(update).reply_text(chunk)

def _require_admin(update: Update) -> bool:
    tg_id = str(update.effective_user.id)
    return not is_admin(tg_id)

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
    await target_msg(update).reply_text(f"Alert (ID {aid}) reset (last_fired_at=NULL, last_met=FALSE).")

async def cmd_forcealert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _require_admin(update):
        await target_msg(update).reply_text("Admins only.")
        return
    if not context.args:
        await target_msg(update).reply_text("Usage: /forcealert <id>")
        return
    try:
        aid = int(context.args[0])
    except Exception:
        await target_msg(update).reply_text("Bad id")
        return
    with session_scope() as session:
        r = session.execute(text("""
            SELECT a.id, a.symbol, a.rule, a.value, a.user_id, u.telegram_id
            FROM alerts a LEFT JOIN users u ON u.id=a.user_id
            WHERE a.id=:id
        """), {"id": aid}).first()
        if not r:
            await target_msg(update).reply_text(f"Alert {aid} not found")
            return
        chat_id = str(r.telegram_id) if r.telegram_id else None
        if not chat_id:
            await target_msg(update).reply_text("No telegram_id for this user; cannot send.")
            return
        try:
            textmsg = f"🔔 (force) Alert (ID {r.id}) | {r.symbol} {r.rule} {r.value}"
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            rr = requests.post(url, json={"chat_id": chat_id, "text": textmsg}, timeout=10)
            if rr.status_code == 200:
                with session_scope() as s2:
                    s2.execute(text("UPDATE alerts SET last_fired_at = NOW(), last_met = TRUE WHERE id=:id"), {"id": aid})
                await target_msg(update).reply_text("Force sent ok. status=200")
            else:
                await target_msg(update).reply_text(f"Force send failed: {rr.status_code} {rr.text[:200]}")
        except Exception as e:
            await target_msg(update).reply_text(f"Force send exception: {e}")

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
        await cmd_help(update, context)
        return
    if data == "go:myalerts":
        await cmd_myalerts(update, context)
        return
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

    if data.startswith("ack:"):
        try:
            _, action, sid = data.split(":", 2)
            aid = int(sid)
        except Exception:
            await query.edit_message_text("Bad action format.")
            return
        try:
            with session_scope() as session:
                if action == "keep":
                    await query.edit_message_reply_markup(reply_markup=None)
                    await query.message.reply_text("✅ Kept. The alert will continue to run.")
                    return
                elif action == "del":
                    row = session.execute(text("SELECT user_seq, user_id FROM alerts WHERE id=:id"), {"id": aid}).first()
                    if not row or row.user_id != plan.user_id:
                        await query.message.reply_text("Not yours or not found.")
                        return
                    seq_txt = f"A{row.user_seq}" if (row and row.user_seq is not None) else f"#{aid}"
                    session.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
                                    {"id": aid, "uid": plan.user_id})
                    await query.edit_message_reply_markup(reply_markup=None)
                    await query.message.reply_text(f"🗑️ Deleted {seq_txt}.")
                    return
                else:
                    await query.edit_message_text("Unknown action.")
                    return
        except Exception as e:
            await query.message.reply_text(f"Action error: {e}")
            return

    if data.startswith("del:"):
        try:
            aid = int(data.split(":", 1)[1])
        except Exception:
            await query.edit_message_text("Bad id.")
            return
        with session_scope() as session:
            owner = session.execute(text("SELECT user_id, user_seq FROM alerts WHERE id=:id"), {"id": aid}).first()
            if not owner:
                await query.edit_message_text("Alert not found.")
                return
            if owner.user_id != plan.user_id:
                await query.edit_message_text("You can delete only your own alerts.")
                return
            session.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"), {"id": aid, "uid": plan.user_id})
        await query.edit_message_text(f"✅ Deleted alert A{owner.user_seq if owner.user_seq is not None else aid}.")

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
        app.add_handler(CommandHandler("forcealert", cmd_forcealert))
        app.add_handler(CommandHandler("runalerts", cmd_runalerts))

        # Extra feature commands
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
    # Core DB init + extras DB (user_settings)
    init_db()
    init_extras()

    # Health & heartbeats (single service)
    start_health_server()
    threading.Thread(target=bot_heartbeat_loop, daemon=True).start()

    # Alerts evaluator loop
    threading.Thread(target=alerts_loop, daemon=True).start()

    # Optional: background pump watcher (reads user opt-ins from user_settings)
    start_pump_watcher()

    # Telegram bot
    run_bot()

if __name__ == "__main__":
    main()
