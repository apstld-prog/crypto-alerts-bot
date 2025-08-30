#!/usr/bin/env python3
import os, logging, threading, asyncio
from flask import Flask, request, send_from_directory, Response
from waitress import serve
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --------------- CONFIG ---------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # e.g. 8290...:AA...
PUBLIC_URL = os.getenv("PUBLIC_URL")  # e.g. https://crypto-alerts-bot-k8i7.onrender.com
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("Missing or invalid BOT_TOKEN")
if not PUBLIC_URL or not PUBLIC_URL.startswith("http"):
    raise RuntimeError("Set PUBLIC_URL to your public Render URL, e.g. https://crypto-alerts-bot-k8i7.onrender.com")

WEBHOOK_PATH = f"/telegram/{BOT_TOKEN}"           # secret-ish path
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}"       # full URL

logging.basicConfig(level=logging.INFO)

# --------------- FLASK APP ---------------
app = Flask(__name__)

@app.after_request
def _headers(resp: Response):
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp

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

# Friendly GET probe (ώστε ο browser να μην δείχνει 405)
@app.get(WEBHOOK_PATH)
def webhook_get_probe():
    return "Telegram webhook endpoint (POST only).", 200

# --------------- TELEGRAM APP ---------------
# Import τους ΠΡΑΓΜΑΤΙΚΟΥΣ handlers από το bot.py σου
from bot import start as start_cmd, help_cmd, price, diagprice

application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("price", price))
application.add_handler(CommandHandler("diagprice", diagprice))

# Init + set webhook (χωρίς polling)
async def tg_init_and_set_webhook():
    await application.initialize()
    # σβήσε τυχόν παλιό webhook & pending updates
    await application.bot.delete_webhook(drop_pending_updates=True)
    # βάλε νέο webhook στο PUBLIC_URL/telegram/<BOT_TOKEN>
    await application.bot.set_webhook(url=WEBHOOK_URL)
    await application.start()   # start PTB internal machinery (no polling)

async def tg_shutdown():
    try:
        await application.stop()
        await application.shutdown()
    except Exception:
        pass

# Webhook route: δέχεται POST από Telegram
@app.post(WEBHOOK_PATH)
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return "bad request", 400
    update = Update.de_json(data, application.bot)
    application.update_queue.put_nowait(update)
    return "ok", 200

# --------------- RUNNERS ---------------
def run_flask():
    logging.info("Starting Flask on %s:%s", HOST, PORT)
    serve(app, host=HOST, port=PORT)

def run_asyncio_loop(loop):
    import asyncio as _asyncio
    _asyncio.set_event_loop(loop)
    loop.run_until_complete(tg_init_and_set_webhook())
    loop.run_forever()

if __name__ == "__main__":
    # 1) Start Telegram async loop (no polling)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=run_asyncio_loop, args=(loop,), daemon=True, name="tg-loop")
    t.start()

    # 2) Start Flask (web server)
    try:
        run_flask()
    finally:
        loop.call_soon_threadsafe(lambda: asyncio.create_task(tg_shutdown()))
        loop.call_soon_threadsafe(loop.stop)
