#!/usr/bin/env python3
import os, logging, threading, asyncio, json
from flask import Flask, request, send_from_directory, Response
from waitress import serve
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============== CONFIG ==============
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
PUBLIC_URL  = os.getenv("PUBLIC_URL", "")  # e.g. https://your-app.onrender.com
HOST        = os.getenv("HOST", "0.0.0.0")
PORT        = int(os.getenv("PORT", "8000"))

if ":" not in BOT_TOKEN:
    raise RuntimeError("Missing or invalid BOT_TOKEN")
if not (PUBLIC_URL.startswith("http://") or PUBLIC_URL.startswith("https://")):
    raise RuntimeError("PUBLIC_URL must be your full Render URL, e.g. https://<service>.onrender.com")

WEBHOOK_PATH = f"/telegram/{BOT_TOKEN}"      # secret-ish path
WEBHOOK_URL  = f"{PUBLIC_URL}{WEBHOOK_PATH}" # full webhook endpoint

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("server")

# ============== FLASK APP ==============
app = Flask(__name__)

@app.after_request
def _headers(resp: Response):
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp

@app.get("/")
def root():
    return (
        "<h3>Crypto Alerts Bot</h3>"
        "<p>Health: <a href='/healthz'>/healthz</a></p>"
        "<p>Webhook probe: <a href='{w}'>GET {w}</a></p>".format(w=WEBHOOK_PATH),
        200,
    )

@app.get("/health")
@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/subscribe.html")
def subscribe_live():
    if os.path.isfile("subscribe.html"):
        return send_from_directory(".", "subscribe.html")
    return ("<h3>Subscribe (LIVE)</h3><p>Place subscribe.html in project root.</p>", 200)

@app.get("/subscribe-sandbox.html")
def subscribe_sandbox():
    if os.path.isfile("subscribe-sandbox.html"):
        return send_from_directory(".", "subscribe-sandbox.html")
    return ("<h3>Subscribe (SANDBOX)</h3><p>Place subscribe-sandbox.html in project root.</p>", 200)

# Friendly GET probe so browser δεν δείχνει 405
@app.get(WEBHOOK_PATH)
def webhook_get_probe():
    return "Telegram webhook endpoint (POST only).", 200

# ============== TELEGRAM APP ==============
# Import τους ΠΡΑΓΜΑΤΙΚΟΥΣ handlers από το bot.py
# ΠΡΟΣΟΧΗ: Τα ονόματα πρέπει να ταιριάζουν ακριβώς με αυτά στο bot.py σου.
from bot import start as start_cmd, help_cmd, price, diagprice  # <-- προσαρμοσμένα στους δικούς σου handlers

application = Application.builder().token(BOT_TOKEN).build()

# Δένουμε τις εντολές
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("price", price))
application.add_handler(CommandHandler("diagprice", diagprice))

# Init + set webhook (χωρίς polling)
async def tg_init_and_set_webhook():
    # Initialize internal machinery
    await application.initialize()

    # Καθάρισε παλιό webhook & pending updates (ασφαλές)
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        log.info("deleteWebhook OK")
    except Exception as e:
        log.warning("deleteWebhook failed: %s", e)

    # Θέσε νέο webhook
    await application.bot.set_webhook(url=WEBHOOK_URL)
    log.info("setWebhook OK → %s", WEBHOOK_URL)

    # Start PTB (χωρίς polling, μόνο για processing via update_queue)
    await application.start()
    log.info("Telegram application started (webhook mode).")

async def tg_shutdown():
    try:
        await application.stop()
        await application.shutdown()
        log.info("Telegram application shutdown complete.")
    except Exception as e:
        log.warning("Telegram shutdown error: %s", e)

# Το endpoint που δέχεται τα updates από Telegram (POST only)
@app.post(WEBHOOK_PATH)
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=False)
        # optional debug log (μικρό)
        log.info("Webhook POST received: keys=%s", list(data.keys()) if isinstance(data, dict) else type(data))
    except Exception:
        return "bad request", 400

    try:
        update = Update.de_json(data, application.bot)
        # push update στην PTB ουρά για async handling
        application.update_queue.put_nowait(update)
    except Exception as e:
        log.exception("Failed to enqueue update: %s", e)
        return "error", 500

    return "ok", 200

# ============== RUN LOOPS ==============
def run_flask():
    log.info("Starting Flask on %s:%s", HOST, PORT)
    serve(app, host=HOST, port=PORT)

def run_asyncio_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(tg_init_and_set_webhook())
    # κρατάμε το loop ζωντανό, PTB διαβάζει από update_queue
    loop.run_forever()

if __name__ == "__main__":
    # 1) Start Telegram async loop (webhook mode, no polling)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=run_asyncio_loop, args=(loop,), daemon=True, name="tg-loop")
    t.start()

    # 2) Start Flask web server (health + webhook)
    try:
        run_flask()
    finally:
        # graceful shutdown
        loop.call_soon_threadsafe(lambda: asyncio.create_task(tg_shutdown()))
        loop.call_soon_threadsafe(loop.stop)
