import os, time, threading, re
from datetime import datetime
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict
from sqlalchemy import select, text
from db import init_db, session_scope, User, Alert, Subscription, engine
from worker_logic import run_alert_cycle, resolve_symbol, fetch_price_binance

# ───────── ENV ─────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_URL = os.getenv("WEB_URL")
ADMIN_KEY = os.getenv("ADMIN_KEY")
INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS","60"))
PAYPAL_SUBSCRIBE_URL = os.getenv("PAYPAL_SUBSCRIBE_URL")
FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT","3"))

RUN_BOT = os.getenv("RUN_BOT", "1") == "1"
RUN_ALERTS = os.getenv("RUN_ALERTS", "1") == "1"

# 🔐 Admins: comma-separated Telegram user IDs
_ADMIN_IDS = {s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

def is_admin(tg_id: str | None) -> bool:
    return (tg_id or "") in _ADMIN_IDS

# ───────── Advisory Locks (Postgres) ─────────
BOT_LOCK_ID = 911001
ALERTS_LOCK_ID = 911002

def try_advisory_lock(lock_id: int) -> bool:
    try:
        with engine.connect() as conn:
            res = conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}).scalar()
            return bool(res)
    except Exception as e:
        print({"msg": "advisory_lock_error", "error": str(e)})
        return False

# ───────── Helpers ─────────
def upgrade_keyboard():
    if PAYPAL_SUBSCRIBE_URL:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Upgrade with PayPal", url=PAYPAL_SUBSCRIBE_URL)]])
    return None

def start_text(limit: int) -> str:
    return (
        "Crypto Alerts Bot\n"
        "Fast prices • Diagnostics • Alerts\n\n"
        "Getting Started:\n"
        "• /price BTC — current price\n"
        "• /setalert BTC > 110000 — alert when condition is met\n"
        "• /myalerts — list your active alerts\n"
        "• /help — instructions\n\n"
        f"Premium: unlimited alerts. Free: up to {limit}."
    )

def safe_chunks(s: str, limit: int = 3900):
    while s:
        yield s[:limit]
        s = s[limit:]

HELP_TEXT = (
    "Help\n\n"
    "• /price <SYMBOL> → Spot price. Example: /price BTC\n"
    "• /setalert <SYMBOL> <op> <value> → Ops: >, < (e.g. /setalert BTC > 110000)\n"
    "• /myalerts → Show active alerts\n"
    "• /cancel_autorenew → Stop future billing (keeps access till period end)\n"
    "• /whoami → shows if you are admin/premium\n"
    "Admin only: /adminstats, /adminsubs, /admincheck, /testalert, /resetalert <id>, /forcealert <id>\n"
)

# ───────── Commands ─────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id==tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
        if is_admin(tg_id) and not user.is_premium:
            user.is_premium = True  # admins always premium
        session.add(user); session.flush()
    lim = 9999 if is_admin(tg_id) else FREE_ALERT_LIMIT
    await update.message.reply_text(start_text(lim), reply_markup=upgrade_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for chunk in safe_chunks(HELP_TEXT):
        await update.message.reply_text(chunk, reply_markup=upgrade_keyboard())

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    role = "admin" if is_admin(tg_id) else "user"
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id==tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
        if is_admin(tg_id):
            user.is_premium = True
        session.add(user); session.flush()
        prem = bool(user.is_premium)
    await update.message.reply_text(f"You are: {role}\nPremium: {prem}")

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price <SYMBOL>  e.g. /price BTC")
        return
    pair = resolve_symbol(context.args[0])
    if not pair:
        await update.message.reply_text("Unknown symbol. Try BTC, ETH, SOL, XRP, SHIB, PEPE ...")
        return
    price = fetch_price_binance(pair)
    if price is None:
        await update.message.reply_text("Price fetch failed. Try again later.")
        return
    await update.message.reply_text(f"{pair}: {price:.6f} USDT")

ALERT_RE = re.compile(r"^(?P<sym>[A-Za-z0-9/]+)\s*(?P<op>>|<)\s*(?P<val>[0-9]+(\.[0-9]+)?)$")

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setalert <SYMBOL> <op> <value>\nExample: /setalert BTC > 110000")
        return
    m = ALERT_RE.match(" ".join(context.args))
    if not m:
        await update.message.reply_text("Format error. Example: /setalert BTC > 110000")
        return
    sym, op, val = m.group("sym"), m.group("op"), float(m.group("val"))
    pair = resolve_symbol(sym)
    if not pair:
        await update.message.reply_text("Unknown symbol. Try BTC, ETH, SOL, XRP, SHIB, PEPE ...")
        return
    rule = "price_above" if op==">" else "price_below"
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id==tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
        if is_admin(tg_id):
            user.is_premium = True  # admin bypass limit
        session.add(user); session.flush()

        if not user.is_premium and not is_admin(tg_id):
            active_alerts = session.execute(
                text("SELECT COUNT(*) FROM alerts WHERE user_id=:uid AND enabled = TRUE"),
                {"uid": user.id}
            ).scalar_one()
            if active_alerts >= FREE_ALERT_LIMIT:
                await update.message.reply_text(f"Free plan limit reached ({FREE_ALERT_LIMIT}). Upgrade for unlimited.")
                return

        alert = Alert(user_id=user.id, symbol=pair, rule=rule, value=val, cooldown_seconds=900)
        session.add(alert); session.flush()
        aid = alert.id
    await update.message.reply_text(f"Alert #{aid} set: {pair} {op} {val}")

async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id==tg_id)).scalar_one_or_none()
        if not user:
            await update.message.reply_text("No alerts yet."); return
        rows = session.execute(text(
            "SELECT id, symbol, rule, value, enabled FROM alerts WHERE user_id=:uid ORDER BY id DESC LIMIT 20"
        ), {"uid": user.id}).all()
    if not rows:
        await update.message.reply_text("No alerts in DB.")
        return
    lines = []
    for r in rows:
        op = ">" if r.rule == "price_above" else "<"
        lines.append(f"• #{r.id} {r.symbol} {op} {r.value} {'ON' if r.enabled else 'OFF'}")
    msg = "Your alerts:\n" + "\n".join(lines)
    for chunk in safe_chunks(msg):
        await update.message.reply_text(msg)

async def cmd_cancel_autorenew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEB_URL or not ADMIN_KEY:
        await update.message.reply_text("Cancel not available right now. Try again later.")
        return
    tg_id = str(update.effective_user.id)
    try:
        r = requests.post(f"{WEB_URL}/billing/paypal/cancel", params={"telegram_id": tg_id, "key": ADMIN_KEY}, timeout=20)
        if r.status_code == 200:
            data = r.json()
            until = data.get("keeps_access_until")
            if until:
                await update.message.reply_text(f"Auto-renew cancelled. Premium active until: {until}")
            else:
                await update.message.reply_text("Auto-renew cancelled. Premium remains active till end of period.")
        else:
            await update.message.reply_text(f"Cancel failed: {r.text}")
    except Exception as e:
        await update.message.reply_text(f"Cancel error: {e}")

# ───────── Admin-only commands ─────────
def _require_admin(update: Update) -> str | None:
    tg_id = str(update.effective_user.id)
    if not is_admin(tg_id):
        return tg_id
    return None

async def cmd_adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    not_admin = _require_admin(update)
    if not_admin:
        await update.message.reply_text("Admins only."); return

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
            subs_active = session.execute(text(
                "SELECT COUNT(*) FROM subscriptions WHERE status_internal = 'ACTIVE'"
            )).scalar_one()
            subs_cancel_at_period_end = session.execute(text(
                "SELECT COUNT(*) FROM subscriptions WHERE status_internal = 'CANCEL_AT_PERIOD_END'"
            )).scalar_one()
            subs_cancelled = session.execute(text(
                "SELECT COUNT(*) FROM subscriptions WHERE status_internal = 'CANCELLED'"
            )).scalar_one()
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
        await update.message.reply_text(msg)

async def cmd_adminsubs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    not_admin = _require_admin(update)
    if not_admin:
        await update.message.reply_text("Admins only."); return

    with session_scope() as session:
        try:
            rows = session.execute(text("""
                SELECT s.id, s.user_id, s.provider, s.status_internal, s.provider_status,
                       COALESCE(s.provider_ref,'') AS provider_ref,
                       s.current_period_end, s.created_at,
                       u.telegram_id
                FROM subscriptions s
                LEFT JOIN users u ON u.id = s.user_id
                ORDER BY s.id DESC
                LIMIT 20
            """)).all()
        except Exception as e:
            await update.message.reply_text(f"subscriptions query error: {e}")
            return

    if not rows:
        await update.message.reply_text("No subscriptions in DB."); return

    lines = []
    for r in rows:
        cpe = r.current_period_end.isoformat() if r.current_period_end else "-"
        lines.append(
            f"#{r.id} uid={r.user_id or '-'} tg={r.telegram_id or '-'} "
            f"{r.status_internal} ({r.provider_status}) ref={r.provider_ref or '-'} cpe={cpe}"
        )
    msg = "Last 20 subscriptions:\n" + "\n".join(lines)
    for chunk in safe_chunks(msg):
        await update.message.reply_text(msg)

async def cmd_admincheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    not_admin = _require_admin(update)
    if not_admin:
        await update.message.reply_text("Admins only."); return
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
                op = ">" if r.rule == "price_above" else "<"
                lines.append(
                    f"  #{r.id} uid={r.user_id} tg={r.telegram_id or '-'} "
                    f"{r.symbol} {op} {r.value} {'ON' if r.enabled else 'OFF'} "
                    f"last_fired={r.last_fired_at} last_met={r.last_met}"
                )
        else:
            lines.append("  (none)")
        for chunk in safe_chunks("\n".join(lines)):
            await update.message.reply_text(chunk)
    except Exception as e:
        await update.message.reply_text(f"admincheck error: {e}")

async def cmd_testalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": tg_id, "text": "Test alert ✅"}, timeout=10)
        await update.message.reply_text(f"testalert status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        await update.message.reply_text(f"testalert exception: {e}")

# NEW: admin reset & force
async def cmd_resetalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    not_admin = _require_admin(update)
    if not_admin:
        await update.message.reply_text("Admins only."); return
    if not context.args:
        await update.message.reply_text("Usage: /resetalert <id>"); return
    try:
        aid = int(context.args[0])
    except Exception:
        await update.message.reply_text("Bad id"); return

    with session_scope() as session:
        row = session.execute(text("SELECT id FROM alerts WHERE id=:id"), {"id": aid}).first()
        if not row:
            await update.message.reply_text(f"Alert {aid} not found"); return
        session.execute(text("UPDATE alerts SET last_fired_at = NULL, last_met = FALSE WHERE id=:id"), {"id": aid})
    await update.message.reply_text(f"Alert #{aid} reset (last_fired_at=NULL, last_met=FALSE).")

async def cmd_forcealert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    not_admin = _require_admin(update)
    if not_admin:
        await update.message.reply_text("Admins only."); return
    if not context.args:
        await update.message.reply_text("Usage: /forcealert <id>"); return
    try:
        aid = int(context.args[0])
    except Exception:
        await update.message.reply_text("Bad id"); return

    with session_scope() as session:
        r = session.execute(text("""
            SELECT a.id, a.symbol, a.rule, a.value, a.user_id, u.telegram_id
            FROM alerts a LEFT JOIN users u ON u.id=a.user_id
            WHERE a.id=:id
        """), {"id": aid}).first()
        if not r:
            await update.message.reply_text(f"Alert {aid} not found"); return
        chat_id = str(r.telegram_id) if r.telegram_id else None
        if not chat_id:
            await update.message.reply_text("No telegram_id for this user; cannot send."); return
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            textmsg = f"🔔 (force) Alert #{r.id} | {r.symbol} {r.rule} {r.value}"
            rq = requests.post(url, json={"chat_id": chat_id, "text": textmsg}, timeout=10)
            if rq.status_code == 200:
                session.execute(text("UPDATE alerts SET last_fired_at = NOW(), last_met = TRUE WHERE id=:id"), {"id": aid})
                await update.message.reply_text(f"Force sent ok. status=200")
            else:
                await update.message.reply_text(f"Force send failed: {rq.status_code} {rq.text[:200]}")
        except Exception as e:
            await update.message.reply_text(f"Force send exception: {e}")

# ───────── Alerts loop (τρέχει μόνο αν πάρουμε lock) ─────────
def alerts_loop():
    if not RUN_ALERTS:
        print({"msg": "alerts_disabled_env"}); return
    if not try_advisory_lock(ALERTS_LOCK_ID):
        print({"msg": "alerts_lock_skipped"}); return
    print({"msg": "alerts_loop_start", "interval": INTERVAL_SECONDS})
    init_db()
    while True:
        ts = datetime.utcnow().isoformat()
        try:
            with session_scope() as session:
                counters = run_alert_cycle(session)
            print({"msg": "alert_cycle", "ts": ts, **counters})
        except Exception as e:
            print({"msg": "alert_cycle_error", "ts": ts, "error": str(e)})
        time.sleep(INTERVAL_SECONDS)

def delete_webhook_if_any():
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
        r = requests.get(url, timeout=10)
        print({"msg": "delete_webhook", "status": r.status_code, "body": r.text[:200]})
    except Exception as e:
        print({"msg": "delete_webhook_error", "error": str(e)})

# ───────── Main ─────────
def main():
    # Alerts σε background thread (με lock)
    t = threading.Thread(target=alerts_loop, daemon=True)
    t.start()

    # Bot στο main thread, μόνο αν έχουμε lock
    if not RUN_BOT:
        print({"msg": "bot_disabled_env"}); return
    if not try_advisory_lock(BOT_LOCK_ID):
        print({"msg": "bot_lock_skipped"})
        while True: time.sleep(3600)

    init_db()
    delete_webhook_if_any()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("myalerts", cmd_myalerts))
    app.add_handler(CommandHandler("cancel_autorenew", cmd_cancel_autorenew))
    # Admin
    app.add_handler(CommandHandler("adminstats", cmd_adminstats))
    app.add_handler(CommandHandler("adminsubs", cmd_adminsubs))
    app.add_handler(CommandHandler("admincheck", cmd_admincheck))
    app.add_handler(CommandHandler("testalert", cmd_testalert))
    app.add_handler(CommandHandler("resetalert", cmd_resetalert))
    app.add_handler(CommandHandler("forcealert", cmd_forcealert))

    print({"msg": "bot_start"})

    while True:
        try:
            app.run_polling(allowed_updates=None, drop_pending_updates=False)
            break
        except Conflict as e:
            print({"msg": "bot_conflict_retry", "error": str(e)})
            time.sleep(30)

if __name__ == "__main__":
    main()
