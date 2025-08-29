#!/usr/bin/env python3
import os, logging, threading, asyncio
from functools import partial

from flask import Flask, request, send_from_directory, Response
from waitress import serve

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------- CONFIG ----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # e.g., https://crypto-alerts-bot-k8i7.onrender.com
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("Missing or invalid BOT_TOKEN")
if not PUBLIC_URL or not PUBLIC_URL.startswith("http"):
    raise RuntimeError("Set PUBLIC_URL to your public Render URL")

WEBHOOK_PATH = f"/telegram/{BOT_TOKEN}"          # secret-ish path
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}"      # full webhook url

logging.basicConfig(level=logging.INFO)

# ---------------------- FLASK APP ----------------------
app_flask = Flask(__name__)

@app_flask.after_request
def _headers(resp: Response):
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp

@app_flask.get("/health")
@app_flask.get("/healthz")
def healthz():
    return "ok", 200

@app_flask.get("/subscribe.html")
def subscribe_live():
    if os.path.isfile("subscribe.html"):
        return send_from_directory(".", "subscribe.html")
    return ("<h3>Subscribe (LIVE)</h3><p>Place a subscribe.html in root.</p>", 200)

@app_flask.get("/subscribe-sandbox.html")
def subscribe_sandbox():
    if os.path.isfile("subscribe-sandbox.html"):
        return send_from_directory(".", "subscribe-sandbox.html")
    return ("<h3>Subscribe (SANDBOX)</h3><p>Place a subscribe-sandbox.html in root.</p>", 200)

# ---------------------- TELEGRAM BOT (webhook) ----------------------
# -- ΒΑΣΙΚΟΙ HANDLERS (προσαρμόσ’ τους όπως στον δικό σου bot.py) --
HELP_TEXT = (
    "👋 *Welcome to Crypto Alerts Bot!*\n\n"
    "Commands:\n"
    "• `/price BTC` — current price (USD)\n"
    "• `/diagprice BTC` — provider diagnostics\n"
    "• `/help` — this help\n\n"
    "Tip: If you see `(stale)`, the price is last known (≤5 min)."
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btn = [[{"text": "Upgrade with PayPal", "url": os.getenv("PAYPAL_SUBSCRIBE_PAGE", "#")}]]
    await update.message.reply_text(
        HELP_TEXT, parse_mode="Markdown",
        reply_markup={"inline_keyboard": btn}
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

# εδώ μπορείς να καλέσεις τις δικές σου συναρτήσεις price/diagprice αν τις έχεις σε άλλο αρχείο
async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Example: /price BTC (wire up your price resolver here)")

async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Diagnostic example (wire real providers)")

# Δημιουργούμε το PTB Application (χωρίς polling)
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("help", cmd_help))
application.add_handler(CommandHandler("price", cmd_price))
application.add_handler(CommandHandler("diagprice", cmd_diag))

# PTB needs to run its internal async loop (initialize/start), αλλά ΔΕΝ κάνουμε polling.
async def tg_init_and_set_webhook():
    await application.initialize()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url=WEBHOOK_URL)

async def tg_shutdown():
    try:
        await application.stop()
        await application.shutdown()
    except Exception:
        pass

# Flask route που δέχεται τα updates από Telegram
@app_flask.post(WEBHOOK_PATH)
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return "bad request", 400
    update = Update.de_json(data, application.bot)
    # βάζουμε το update στην ουρά του PTB για επεξεργασία
    application.update_queue.put_nowait(update)
    return "ok", 200

# ---------------------- RUNNERS ----------------------
def run_flask():
    logging.info("Starting Flask on %s:%s", HOST, PORT)
    serve(app_flask, host=HOST, port=PORT)

def run_asyncio_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(tg_init_and_set_webhook())
    # keep the loop running forever; PTB processes updates via update_queue
    loop.run_forever()

if __name__ == "__main__":
    # 1) Start Telegram async loop (no polling)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=run_asyncio_loop, args=(loop,), daemon=True, name="tg-loop")
    t.start()

    # 2) Start Flask (exposes /healthz and the webhook endpoint)
    try:
        run_flask()
    finally:
        # on shutdown
        loop.call_soon_threadsafe(lambda: asyncio.create_task(tg_shutdown()))
        loop.call_soon_threadsafe(loop.stop)
