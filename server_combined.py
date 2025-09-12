# server_combined.py
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

_BOT_HEART_INTERVAL = int(os.getenv("BOT_HEART_INTERVAL_SECONDS", "60"))
_BOT_HEART_TTL = int(os.getenv("BOT_HEART_TTL_SECONDS", "180"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

def is_admin(tg_id: str | None) -> bool:
    return (tg_id or "") in _ADMIN_IDS

def try_advisory_lock(lock_id: int) -> bool:
    try:
        with engine.connect() as conn:
            return bool(conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}).scalar())
    except Exception:
        return False

health_app = FastAPI()

_BOT_HEART_BEAT_AT: datetime | None = None
_BOT_HEART_STATUS: str = "unknown"
_ALERTS_LAST_OK_AT: datetime | None = None
_ALERTS_LAST_RESULT: dict | None = None

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

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    def _run():
        uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="info")
    threading.Thread(target=_run, daemon=True).start()
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
        except Exception:
            _BOT_HEART_STATUS = "fail"
            _BOT_HEART_BEAT_AT = datetime.utcnow()
        time.sleep(_BOT_HEART_INTERVAL)

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

# ───────────────── Commands ─────────────────
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
    return (
        "<b>Crypto Alerts Bot</b>\n"
        "⚡️ <i>Fast prices</i> • 🧪 <i>Diagnostics</i> • 🔔 <i>Alerts</i>\n\n"
        "<b>Getting Started</b>\n"
        "• <code>/price BTC</code>\n"
        "• <code>/setalert BTC &gt; 110000</code>\n"
        "• <code>/myalerts</code>\n"
        "• <code>/help</code>\n"
        "• <code>/support &lt;message&gt;</code>\n\n"
        f"Free: <b>{limit}</b> alerts • Premium: unlimited."
    )

HELP_TEXT_HTML = (
    "<b>Help</b>\n\n"
    "• <code>/price &lt;SYMBOL&gt;</code>\n"
    "• <code>/setalert &lt;SYMBOL&gt; &lt;op&gt; &lt;value&gt;</code> (>, <)\n"
    "• <code>/myalerts</code>\n"
    "• <code>/delalert &lt;id&gt;</code>\n"
    "• <code>/clearalerts</code>\n"
    "• <code>/cancel_autorenew</code>\n"
    "• <code>/support &lt;message&gt;</code>\n"
    "• <code>/whoami</code>\n"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
        if is_admin(tg_id) and not user.is_premium:
            user.is_premium = True
        session.add(user); session.flush()
        user_limit = 9999 if is_admin(tg_id) else FREE_ALERT_LIMIT
    await target_msg(update).reply_text(
        start_text(user_limit),
        reply_markup=main_menu_keyboard(tg_id),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    for chunk in safe_chunks(HELP_TEXT_HTML):
        await target_msg(update).reply_text(
            chunk,
            reply_markup=upgrade_keyboard(tg_id),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
        if is_admin(tg_id):
            user.is_premium = True
        session.add(user); session.flush()
        prem = bool(user.is_premium)
        role = "admin" if is_admin(tg_id) else "user"
    await target_msg(update).reply_text(f"You are: {role}\nPremium: {prem}")

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = (context.args[0] if context.args else "BTC").upper()
    pair = resolve_symbol(symbol)
    if not pair:
        await target_msg(update).reply_text("Unknown symbol.")
        return
    price = fetch_price_binance(pair)
    if price is None:
        await target_msg(update).reply_text("Price fetch failed.")
        return
    await target_msg(update).reply_text(f"{pair}: {price:.6f} USDT")

ALERT_RE = re.compile(r"^(?P<sym>[A-Za-z0-9/]+)\s*(?P<op>>|<)\s*(?P<val>[0-9]+(\.[0-9]+)?)$")

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await target_msg(update).reply_text("Usage: /setalert <SYMBOL> <op> <value>")
        return
    m = ALERT_RE.match(" ".join(context.args))
    if not m:
        await target_msg(update).reply_text("Format error. Example: /setalert BTC > 110000")
        return
    sym, op, val = m.group("sym"), m.group("op"), float(m.group("val"))
    pair = resolve_symbol(sym)
    if not pair:
        await target_msg(update).reply_text("Unknown symbol.")
        return
    rule = "price_above" if op == ">" else "price_below"
    tg_id = str(update.effective_user.id)

    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
        if is_admin(tg_id):
            user.is_premium = True
        session.add(user); session.flush()
        user_id = user.id

        if not user.is_premium and not is_admin(tg_id):
            active_alerts = session.execute(
                text("SELECT COUNT(*) FROM alerts WHERE user_id=:uid AND enabled = TRUE"),
                {"uid": user_id}
            ).scalar_one()
            if active_alerts >= FREE_ALERT_LIMIT:
                await target_msg(update).reply_text(f"Free plan limit reached ({FREE_ALERT_LIMIT}). Upgrade for unlimited.")
                return

        alert = Alert(user_id=user_id, symbol=pair, rule=rule, value=val, cooldown_seconds=900)
        session.add(alert); session.flush()
        alert_id = alert.id

    await target_msg(update).reply_text(f"✅ Alert #{alert_id} set: {pair} {op} {val}")

def _alert_buttons(aid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑️ Delete #{aid}", callback_data=f"del:{aid}")]])

async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
        if not user:
            await target_msg(update).reply_text("No alerts yet.")
            return
        user_id = user.id
        rows = session.execute(text(
            "SELECT id, symbol, rule, value, enabled FROM alerts WHERE user_id=:uid ORDER BY id DESC LIMIT 20"
        ), {"uid": user_id}).all()
    if not rows:
        await target_msg(update).reply_text("No alerts in DB.")
        return
    for r in rows:
        op = op_from_rule(r.rule)
        txt = f"#{r.id}  {r.symbol} {op} {r.value}  {'ON' if r.enabled else 'OFF'}"
        await target_msg(update).reply_text(txt, reply_markup=_alert_buttons(r.id))

async def cmd_delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    if not context.args:
        await target_msg(update).reply_text("Usage: /delalert <id>")
        return
    try:
        aid = int(context.args[0])
    except Exception:
        await target_msg(update).reply_text("Bad id")
        return
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
        is_premium = bool(user and user.is_premium) or is_admin(tg_id)
        if not is_premium:
            await target_msg(update).reply_text("Premium required to delete alerts.")
            return
        user_id = user.id if user else None
        if is_admin(tg_id):
            res = session.execute(text("DELETE FROM alerts WHERE id=:id"), {"id": aid})
        else:
            res = session.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"), {"id": aid, "uid": user_id})
        deleted = res.rowcount or 0
    await target_msg(update).reply_text(f"Alert #{aid} deleted." if deleted else "Nothing deleted.")

async def cmd_clearalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
        is_premium = bool(user and user.is_premium) or is_admin(tg_id)
        if not is_premium:
            await target_msg(update).reply_text("Premium required to clear alerts.")
            return
        user_id = user.id if user else None
        res = session.execute(text("DELETE FROM alerts WHERE user_id=:uid"), {"uid": user_id})
        deleted = res.rowcount or 0
    await target_msg(update).reply_text(f"Deleted {deleted} alert(s).")

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    if not context.args:
        await target_msg(update).reply_text("Send: /support <your message>")
        return
    msg = " ".join(context.args).strip()
    who = update.effective_user
    header = f"🆘 Support message\nFrom: {who.first_name or ''} (@{who.username}) id={tg_id}"
    send_admins(f"{header}\n\n{msg}")
    await target_msg(update).reply_text("✅ Sent to support.")

async def cmd_testalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": tg_id, "text": "Test alert ✅"}, timeout=10)
    await target_msg(update).reply_text(f"testalert status={r.status_code} body={r.text[:200]}")

def _require_admin(update: Update) -> bool:
    return not is_admin(str(update.effective_user.id))

# (Admin commands trimmed for brevity except runalerts)
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

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Loading...", show_alert=False)
    data = (query.data or "").strip()
    tg_id = str(query.from_user.id)

    if data == "go:myalerts":
        await cmd_myalerts(update, context); return
    if data.startswith("go:price:"):
        sym = data.split(":", 2)[2]
        pair = resolve_symbol(sym)
        price = fetch_price_binance(pair) if pair else None
        await query.message.reply_text("Price fetch failed." if price is None else f"{pair}: {price:.6f} USDT")
        return
    if data == "go:setalerthelp":
        await query.message.reply_text("Examples:\n• /setalert BTC > 110000\n• /setalert ETH < 2000")
        return
    if data == "go:help":
        for chunk in safe_chunks(HELP_TEXT_HTML):
            await query.message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    if data == "go:support":
        await query.message.reply_text("Send a message to support:\n/support <your message>")
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
                user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
                if not user:
                    await query.edit_message_text("User not found.")
                    return
                user_id = user.id
                if action == "keep":
                    await query.edit_message_reply_markup(reply_markup=None)
                    await query.message.reply_text("✅ Kept.")
                    return
                elif action == "del":
                    if is_admin(tg_id):
                        res = session.execute(text("DELETE FROM alerts WHERE id=:id"), {"id": aid})
                    else:
                        res = session.execute(
                            text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
                            {"id": aid, "uid": user_id}
                        )
                    deleted = res.rowcount or 0
                    await query.edit_message_reply_markup(reply_markup=None)
                    await query.message.reply_text("🗑️ Deleted." if deleted else "Nothing deleted.")
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
            user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
            is_premium = bool(user and user.is_premium) or is_admin(tg_id)
            if not is_premium:
                await query.edit_message_text("Premium required to delete alerts.")
                return
            owner = session.execute(text("SELECT user_id FROM alerts WHERE id=:id"), {"id": aid}).first()
            if not owner:
                await query.edit_message_text("Alert not found.")
                return
            if not is_admin(tg_id):
                u = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
                if not u or owner.user_id != u.id:
                    await query.edit_message_text("You can delete only your own alerts.")
                    return
            res = session.execute(text("DELETE FROM alerts WHERE id=:id"), {"id": aid})
            deleted = res.rowcount or 0
        await query.edit_message_text(f"✅ Deleted alert #{aid}." if deleted else "Nothing deleted.")

def alerts_loop():
    global _ALERTS_LAST_OK_AT, _ALERTS_LAST_RESULT
    if not RUN_ALERTS:
        print({"msg": "alerts_disabled_env"})
        return
    if not try_advisory_lock(ALERTS_LOCK_ID):
        print({"msg": "alerts_lock_skipped"})
        return
    print({"msg": "alerts_loop_start", "interval": INTERVAL_SECONDS})
    init_db()
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

def main():
    start_health_server()
    threading.Thread(target=bot_heartbeat_loop, daemon=True).start()
    threading.Thread(target=alerts_loop, daemon=True).start()

    if not RUN_BOT:
        print({"msg": "bot_disabled_env"})
        return
    if not try_advisory_lock(BOT_LOCK_ID):
        print({"msg": "bot_lock_skipped"})
        while True:
            time.sleep(3600)

    init_db()
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
        requests.get(url, timeout=10)
    except Exception:
        pass

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("myalerts", cmd_myalerts))
    app.add_handler(CommandHandler("delalert", cmd_delalert))
    app.add_handler(CommandHandler("clearalerts", cmd_clearalerts))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("testalert", cmd_testalert))
    app.add_handler(CommandHandler("runalerts", cmd_runalerts))
    app.add_handler(CallbackQueryHandler(on_callback))

    print({"msg": "bot_start"})
    while True:
        try:
            app.run_polling(allowed_updates=None, drop_pending_updates=True, poll_interval=0.5, timeout=10)
            break
        except Conflict as e:
            # If another instance is polling, wait and retry.
            print({"msg": "bot_conflict_retry", "error": str(e)})
            time.sleep(30)

if __name__ == "__main__":
    main()
