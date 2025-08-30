#!/usr/bin/env python3
import os, logging, threading, asyncio, traceback
from flask import Flask, request, send_from_directory, Response
from waitress import serve
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ============== CONFIG ==============
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
PUBLIC_URL  = os.getenv("PUBLIC_URL", "")  # e.g. https://your-app.onrender.com
HOST        = os.getenv("HOST", "0.0.0.0")
PORT        = int(os.getenv("PORT", "8000"))

if ":" not in BOT_TOKEN:
    raise RuntimeError("Missing or invalid BOT_TOKEN")
if not (PUBLIC_URL.startswith("http://") or PUBLIC_URL.startswith("https://")):
    raise RuntimeError("PUBLIC_URL must be your full Render URL, e.g. https://<service>.onrender.com")

WEBHOOK_PATH = f"/telegram/{BOT_TOKEN}"
WEBHOOK_URL  = f"{PUBLIC_URL}{WEBHOOK_PATH}"

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
        f"<h3>Crypto Alerts Bot</h3>"
        f"<p>Health: <a href='/healthz'>/healthz</a></p>"
        f"<p>Webhook probe: <a href='{WEBHOOK_PATH}'>GET {WEBHOOK_PATH}</a></p>",
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

@app.get(WEBHOOK_PATH)
def webhook_get_probe():
    return "Telegram webhook endpoint (POST only).", 200

# ============== TELEGRAM APP ==============
# Import ΠΡΑΓΜΑΤΙΚΩΝ handlers από bot.py
from bot import start as start_cmd, help_cmd, price, diagprice

application = Application.builder().token(BOT_TOKEN).build()

# ---- Safe wrappers (reply on error + full logging) ----
async def _safe_call(handler, update: Update, context: ContextTypes.DEFAULT_TYPE, tag: str):
    chat_id = getattr(update.effective_chat, "id", None)
    text = getattr(update.message, "text", None)
    try:
        log.info("→ handling %s (chat_id=%s, text=%r)", tag, chat_id, text)
        await handler(update, context)
        log.info("← done %s", tag)
    except Exception as e:
        log.error("Handler %s crashed: %s\n%s", tag, e, traceback.format_exc())
        try:
            if getattr(update, "message", None):
                await update.message.reply_text("⚠️ Something went wrong while processing your request. Please try again.")
        except Exception as e2:
            log.warning("Failed to send error reply: %s", e2)

async def start_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_call(start_cmd, update, context, "/start")

async def help_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_call(help_cmd, update, context, "/help")

async def price_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_call(price, update, context, f"/price {' '.join(context.args or [])}")

async def diagprice_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_call(diagprice, update, context, f"/diagprice {' '.join(context.args or [])}")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = getattr(update.message, "text", "")
    log.info("→ unknown command: %r (chat_id=%s)", txt, getattr(update.effective_chat, "id", None))
    try:
        await update.message.reply_text("Unknown command. Try /start or /help.")
    except Exception:
        log.warning("Failed to reply unknown command.")

async def catch_all_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = getattr(update.message, "text", "")
    log.info("→ catch-all text: %r (chat_id=%s)", txt, getattr(update.effective_chat, "id", None))
    try:
        await update.message.reply_text("Hi! Use /start to see the instructions.")
    except Exception:
        log.warning("Failed to reply catch-all.")

# Bind handlers
application.add_handler(CommandHandler("start", start_wrap))
application.add_handler(CommandHandler("help", help_wrap))
application.add_handler(CommandHandler("price", price_wrap))
application.add_handler(CommandHandler("diagprice", diagprice_wrap))
application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_all_text))

# Global error handler (PTB-level)
async def on_error(update: object, context):
    try:
        upd = update.to_dict() if hasattr(update, "to_dict") else str(update)
    except Exception:
        upd = str(update)
    log.error("PTB error: %s\nUpdate: %s\nTraceback:\n%s", context.error, upd, traceback.format_exc())

application.add_error_handler(on_error)

# --- Async loop reference for thread-safe dispatch ---
TG_LOOP = None

# Init + set webhook
async def tg_init_and_set_webhook():
    await application.initialize()
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        log.info("deleteWebhook OK")
    except Exception as e:
        log.warning("deleteWebhook failed: %s", e)
    await application.bot.set_webhook(url=WEBHOOK_URL)
    log.info("setWebhook OK → %s", WEBHOOK_URL)
    await application.start()
    log.info("Telegram application started (webhook mode).")

async def tg_shutdown():
    try:
        await application.stop()
        await application.shutdown()
        log.info("Telegram application shutdown complete.")
    except Exception as e:
        log.warning("Telegram shutdown error: %s", e)

# Webhook endpoint — non-blocking dispatch
@app.post(WEBHOOK_PATH)
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=False)
        msg = data.get("message", {}) if isinstance(data, dict) else {}
        log.info("Webhook POST received: keys=%s, text=%r",
                 list(data.keys()) if isinstance(data, dict) else [type(data)],
                 msg.get("text"))
    except Exception:
        return "bad request", 400

    try:
        update = Update.de_json(data, application.bot)
        fut = asyncio.run_coroutine_threadsafe(application.process_update(update), TG_LOOP)

        # Optional: detailed error log if the handler crashes
        def _done_cb(f):
            try:
                f.result()
            except Exception as e:
                log.error("process_update error: %s\n%s", e, traceback.format_exc())
        fut.add_done_callback(_done_cb)

    except Exception as e:
        log.error("Failed to process update: %s\n%s", e, traceback.format_exc())
        return "error", 500

    return "ok", 200

# ============== RUN LOOPS ==============
def run_flask():
    log.info("Starting Flask on %s:%s", HOST, PORT)
    serve(app, host=HOST, port=PORT)

def run_asyncio_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(tg_init_and_set_webhook())
    loop.run_forever()

if __name__ == "__main__":
    TG_LOOP = asyncio.new_event_loop()
    t = threading.Thread(target=run_asyncio_loop, args=(TG_LOOP,), daemon=True, name="tg-loop")
    t.start()
    try:
        run_flask()
    finally:
        TG_LOOP.call_soon_threadsafe(lambda: asyncio.create_task(tg_shutdown()))
        TG_LOOP.call_soon_threadsafe(TG_LOOP.stop)
