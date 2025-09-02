import os, time, threading, re
from datetime import datetime
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from sqlalchemy import select, text
from db import init_db, session_scope, User, Alert, engine
from worker_logic import run_alert_cycle, resolve_symbol, fetch_price_binance

# ───────── ENV ─────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_URL = os.getenv("WEB_URL")
ADMIN_KEY = os.getenv("ADMIN_KEY")
INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS","60"))
PAYPAL_SUBSCRIBE_URL = os.getenv("PAYPAL_SUBSCRIBE_URL")
FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT","3"))

# Προαιρετικά flags για χειροκίνητο έλεγχο ρόλων
RUN_BOT = os.getenv("RUN_BOT", "1") == "1"
RUN_ALERTS = os.getenv("RUN_ALERTS", "1") == "1"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

# ───────── Advisory Locks (Postgres) ─────────
BOT_LOCK_ID = 911001    # μοναδικά ids για locks
ALERTS_LOCK_ID = 911002

def try_advisory_lock(lock_id: int) -> bool:
    """Επιστρέφει True αν πήραμε αποκλειστικό lock, αλλιώς False."""
    try:
        with engine.connect() as conn:
            res = conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}).scalar()
            return bool(res)
    except Exception:
        # Αν τρέχεις SQLite τοπικά, δεν υπάρχει pg_try_advisory_lock.
        # Επιστρέφουμε True ΜΟΝΟ αν ξέρεις ότι τρέχεις ένα instance.
        return True

# ───────── Texts/Keyboards ─────────
START_TEXT = (
    "🧭 *Crypto Alerts Bot*\n"
    "_Fast prices • Diagnostics • Alerts_\n\n"
    "### 🚀 *Getting Started*\n"
    "• `/price BTC` — current price in USD (e.g., `/price ETH`).\n"
    "• `/setalert BTC > 110000` — alert when condition is met.\n"
    "• `/myalerts` — list your active alerts.\n"
    "• `/help` — full instructions.\n\n"
    f"💎 *Premium*: unlimited alerts. *Free*: up to {FREE_ALERT_LIMIT}."
)

HELP_TEXT = (
    "📖 *Help*\n\n"
    "• `/price <SYMBOL>` → Spot price. Example: `/price BTC`.\n"
    "• `/setalert <SYMBOL> <op> <value>` → Ops: `>`, `<` (π.χ. `/setalert BTC > 110000`).\n"
    "• `/myalerts` → Show active alerts.\n"
    "• `/cancel_autorenew` → Stop future billing (keeps access till period end).\n"
)

def upgrade_keyboard():
    if PAYPAL_SUBSCRIBE_URL:
        return InlineKeyboardMarkup([[InlineKeyboardButton("💠 Upgrade with PayPal", url=PAYPAL_SUBSCRIBE_URL)]])
    return None

# ───────── Commands ─────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id==tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
            session.add(user); session.flush()
    await update.message.reply_text(START_TEXT, parse_mode="Markdown", reply_markup=upgrade_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=upgrade_keyboard())

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
    await update.message.reply_text(f"💹 {pair}: *{price:.6f}* USDT", parse_mode="Markdown")

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
            session.add(user); session.flush()
        active_alerts = session.execute(text(
            "SELECT COUNT(*) FROM alerts WHERE user_id=:uid AND enabled=1"
        ), {"uid": user.id}).scalar_one()
        if (not user.is_premium) and active_alerts >= FREE_ALERT_LIMIT:
            await update.message.reply_text(f"Free plan limit reached ({FREE_ALERT_LIMIT}). Upgrade for unlimited.")
            return
        alert = Alert(user_id=user.id, symbol=pair, rule=rule, value=val, cooldown_seconds=900)
        session.add(alert); session.flush()
        aid = alert.id
    await update.message.reply_text(f"✅ Alert #{aid} set: {pair} {op} {val}")

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
        lines.append(f"• #{r.id} {r.symbol} {op} {r.value} {'✅' if r.enabled else '❌'}")
    await update.message.reply_text("🧾 *Your alerts:*\n" + "\n".join(lines), parse_mode="Markdown")

async def cmd_cancel_autorenew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEB_URL or not ADMIN_KEY:
        await update.message.reply_text("⚠️ Cancel not available right now. Try again later.")
        return
    tg_id = str(update.effective_user.id)
    try:
        r = requests.post(f"{WEB_URL}/billing/paypal/cancel", params={"telegram_id": tg_id, "key": ADMIN_KEY}, timeout=20)
        if r.status_code == 200:
            data = r.json()
            until = data.get("keeps_access_until")
            if until:
                await update.message.reply_text(f"✅ Auto-renew cancelled. Premium active until: {until}")
            else:
                await update.message.reply_text("✅ Auto-renew cancelled. Premium remains active till end of period.")
        else:
            await update.message.reply_text(f"❌ Cancel failed: {r.text}")
    except Exception as e:
        await update.message.reply_text(f"❌ Cancel error: {e}")

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
    """Σβήνει τυχόν Telegram webhook πριν ξεκινήσει το polling, για να αποφύγουμε conflicts."""
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
        print({"msg": "bot_disabled_env"})
        return
    if not try_advisory_lock(BOT_LOCK_ID):
        print({"msg": "bot_lock_skipped"})
        # Μένουμε ζωντανοί για να τρέχει το alerts thread
        while True: time.sleep(3600)

    init_db()
    # Σβήσε webhook πριν ξεκινήσει το polling (αν υπάρχει)
    delete_webhook_if_any()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("myalerts", cmd_myalerts))
    app.add_handler(CommandHandler("cancel_autorenew", cmd_cancel_autorenew))
    print({"msg": "bot_start"})
    app.run_polling(allowed_updates=None, drop_pending_updates=False)

if __name__ == "__main__":
    main()
