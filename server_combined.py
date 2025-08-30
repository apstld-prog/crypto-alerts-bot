#!/usr/bin/env python3
import os, logging, threading, asyncio, time, traceback, json
from flask import Flask, request, send_from_directory, Response
from waitress import serve
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import requests

# ======== BASIC CONFIG ========
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
PUBLIC_URL  = os.getenv("PUBLIC_URL", "")
HOST        = os.getenv("HOST", "0.0.0.0")
PORT        = int(os.getenv("PORT", "8000"))
ALERT_INTERVAL_SEC = int(os.getenv("ALERT_INTERVAL_SEC", "0"))

# PayPal LIVE (because you pay live)
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "live").lower()
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")

if ":" not in BOT_TOKEN: raise RuntimeError("Missing or invalid BOT_TOKEN")
if not (PUBLIC_URL.startswith("http://") or PUBLIC_URL.startswith("https://")):
    raise RuntimeError("PUBLIC_URL must be full URL")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("server")

API_BASE = "https://api-m.paypal.com" if PAYPAL_MODE == "live" else "https://api-m.sandbox.paypal.com"

# ======== FLASK ========
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
    return (f"<h3>Crypto Alerts Bot</h3>"
            f"<p>Health: <a href='/healthz'>/healthz</a></p>"
            f"<p>Webhook probe: <a href='{WEBHOOK_PATH}'>GET {WEBHOOK_PATH}</a></p>"
            f"<p>Cron: <a href='/cron'>/cron</a> (call every 1′)</p>", 200)

@app.get("/health")
@app.get("/healthz")
def healthz(): return "ok", 200

@app.get("/subscribe.html")
def subscribe_live():
    if os.path.isfile("subscribe.html"):
        return send_from_directory(".", "subscribe.html")
    return ("<h3>Subscribe (LIVE)</h3><p>Place subscribe.html in project root.</p>", 200)

# ======== TELEGRAM APP ========
from bot import (
    start as start_cmd, help_cmd, premium_cmd, whoami, stats, subs, bindsub, syncsub,
    price, diagprice, setalert, myalerts, delalert, clearalerts,
    resolve_price_usd, get_db_conn, set_premium, set_subscription_record
)

application = Application.builder().token(BOT_TOKEN).build()
WEBHOOK_PATH = f"/telegram/{BOT_TOKEN}"
WEBHOOK_URL  = f"{PUBLIC_URL}{WEBHOOK_PATH}"

async def _safe(handler, tag, update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        log.info("→ handling %s (chat_id=%s, text=%r)", tag, getattr(update.effective_chat, "id", None), getattr(update.message, "text", None))
        await handler(update, context)
        log.info("← done %s", tag)
    except Exception as e:
        log.error("Handler %s crashed: %s\n%s", tag, e, traceback.format_exc())
        try:
            if getattr(update, "message", None):
                await update.message.reply_text("⚠️ Something went wrong. Please try again.")
        except Exception:
            pass

async def start_wrap(u,c):       await _safe(start_cmd, "/start", u, c)
async def help_wrap(u,c):        await _safe(help_cmd, "/help", u, c)
async def premium_wrap(u,c):     await _safe(premium_cmd, "/premium", u, c)
async def whoami_wrap(u,c):      await _safe(whoami, "/whoami", u, c)
async def stats_wrap(u,c):       await _safe(stats, "/stats", u, c)
async def subs_wrap(u,c):        await _safe(subs, "/subs", u, c)
async def bindsub_wrap(u,c):     await _safe(bindsub, "/bindsub", u, c)
async def syncsub_wrap(u,c):     await _safe(syncsub, "/syncsub", u, c)
async def price_wrap(u,c):       await _safe(price, "/price", u, c)
async def diag_wrap(u,c):        await _safe(diagprice, "/diagprice", u, c)
async def setalert_wrap(u,c):    await _safe(setalert, "/setalert", u, c)
async def myalerts_wrap(u,c):    await _safe(myalerts, "/myalerts", u, c)
async def delalert_wrap(u,c):    await _safe(delalert, "/delalert", u, c)
async def clearalerts_wrap(u,c): await _safe(clearalerts, "/clearalerts", u, c)

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Try /start or /help.")
async def catch_all_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! Use /start to see the instructions.")

# Bind
application.add_handler(CommandHandler("start", start_wrap))
application.add_handler(CommandHandler("help", help_wrap))
application.add_handler(CommandHandler("premium", premium_wrap))
application.add_handler(CommandHandler("whoami", whoami_wrap))
application.add_handler(CommandHandler("stats", stats_wrap))
application.add_handler(CommandHandler("subs", subs_wrap))
application.add_handler(CommandHandler("bindsub", bindsub_wrap))
application.add_handler(CommandHandler("syncsub", syncsub_wrap))
application.add_handler(CommandHandler("price", price_wrap))
application.add_handler(CommandHandler("diagprice", diag_wrap))
application.add_handler(CommandHandler("setalert", setalert_wrap))
application.add_handler(CommandHandler("myalerts", myalerts_wrap))
application.add_handler(CommandHandler("delalert", delalert_wrap))
application.add_handler(CommandHandler("clearalerts", clearalerts_wrap))
application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_all_text))

async def on_error(update: object, context):
    try: upd = update.to_dict() if hasattr(update, "to_dict") else str(update)
    except Exception: upd = str(update)
    log.error("PTB error: %s\nUpdate: %s\nTraceback:\n%s", context.error, upd, traceback.format_exc())
application.add_error_handler(on_error)

# Async loop ref
TG_LOOP = None

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

@app.post(f"/telegram/{BOT_TOKEN}")
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
        def _done_cb(f):
            try: f.result()
            except Exception as e:
                log.error("process_update error: %s\n%s", e, traceback.format_exc())
        fut.add_done_callback(_done_cb)
    except Exception as e:
        log.error("Failed to process update: %s\n%s", e, traceback.format_exc())
        return "error", 500

    return "ok", 200

# ======== ALERTS: /cron endpoint ========
def run_alert_tick():
    from bot import resolve_price_usd, get_db_conn
    conn = get_db_conn(); cur = conn.cursor()
    rows = cur.execute("SELECT id,user_id,symbol,op,threshold FROM alerts WHERE active=1 ORDER BY id ASC").fetchall()
    if not rows: return 0
    symbols = {r[2] for r in rows}
    prices = {}
    for s in symbols:
        try:
            p = resolve_price_usd(s)
            if p is not None: prices[s] = float(p)
        except Exception: pass
    triggered = []
    for (aid, uid, sym, op, thr) in rows:
        p = prices.get(sym.lower())
        if p is None: continue
        if (op == ">" and p > thr) or (op == "<" and p < thr):
            triggered.append((aid, uid, sym, op, thr, p))
    for (aid, uid, sym, op, thr, p) in triggered:
        try:
            text = f"🔔 Alert hit: **{sym.upper()} {op} {thr}**\nCurrent price: **${p:.6f}**"
            coro = application.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
            asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
            cur.execute("UPDATE alerts SET active=0 WHERE id=?", (aid,))
            conn.commit()
            log.info("Alert %s delivered to %s and deactivated.", aid, uid)
        except Exception as e:
            log.warning("Failed to notify alert %s: %s", aid, e)
    return len(triggered)

@app.get("/cron")
def cron_tick():
    try:
        n = run_alert_tick()
        return f"cron OK, triggered={n}", 200
    except Exception as e:
        log.error("cron_tick error: %s\n%s", e, traceback.format_exc())
        return "cron error", 500

# ======== PAYPAL HELPERS ========
def paypal_access_token():
    r = requests.post(f"{API_BASE}/v1/oauth2/token",
                      auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                      data={"grant_type":"client_credentials"}, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]

def paypal_get_subscription(sub_id, token=None):
    token = token or paypal_access_token()
    r = requests.get(f"{API_BASE}/v1/billing/subscriptions/{sub_id}",
                     headers={"Authorization": f"Bearer {token}"}, timeout=15)
    r.raise_for_status()
    return r.json()

def paypal_verify_webhook(req_headers, body_bytes):
    token = paypal_access_token()
    verify_payload = {
        "auth_algo": req_headers.get("PayPal-Auth-Algo"),
        "cert_url": req_headers.get("PayPal-Cert-Url"),
        "transmission_id": req_headers.get("PayPal-Transmission-Id"),
        "transmission_sig": req_headers.get("PayPal-Transmission-Sig"),
        "transmission_time": req_headers.get("PayPal-Transmission-Time"),
        "webhook_id": PAYPAL_WEBHOOK_ID,
        "webhook_event": json.loads(body_bytes.decode("utf-8"))
    }
    r = requests.post(f"{API_BASE}/v1/notifications/verify-webhook-signature",
                      headers={"Authorization": f"Bearer {token}",
                              "Content-Type": "application/json"},
                      json=verify_payload, timeout=20)
    r.raise_for_status()
    ok = (r.json().get("verification_status") == "SUCCESS")
    log.info("paypal_verify_webhook → %s", ok)
    return ok

# ======== PAYPAL ROUTES ========
@app.post("/paypal/subscribe-bind")
def paypal_subscribe_bind():
    """
    Called from subscribe.html onApprove to bind subscription_id to Telegram user id
    and fetch initial status from PayPal. If ACTIVE -> set premium immediately.
    """
    try:
        data = request.get_json(force=True, silent=False)
        uid = int(data["uid"])
        sub_id = str(data["subscription_id"])
    except Exception:
        log.error("subscribe-bind bad request: %s", request.data)
        return ("bad request", 400)

    from bot import set_premium, set_subscription_record
    try:
        token = paypal_access_token()
        sub = paypal_get_subscription(sub_id, token=token)
        status = sub.get("status")
        payer_id = (sub.get("subscriber") or {}).get("payer_id")
        plan_id = sub.get("plan_id")
        set_subscription_record(sub_id, uid, status, payer_id, plan_id)
        log.info("subscribe-bind OK: sub_id=%s uid=%s status=%s plan=%s", sub_id, uid, status, plan_id)
        if status == "ACTIVE":
            set_premium(uid, True)
        return {"ok": True, "status": status}, 200
    except Exception as e:
        log.error("subscribe-bind error: %s\n%s", e, traceback.format_exc())
        return ("error", 500)

@app.post("/paypal/webhook")
def paypal_webhook():
    """
    PayPal calls this for subscription lifecycle events.
    We verify signature, update DB, and toggle premium accordingly.
    """
    raw = request.get_data()
    try:
        if not paypal_verify_webhook(request.headers, raw):
            log.warning("PayPal webhook verification FAILED")
            return "not verified", 400
        event = json.loads(raw.decode("utf-8"))
    except Exception as e:
        log.error("webhook parse/verify error: %s\n%s", e, traceback.format_exc())
        return "bad request", 400

    et = event.get("event_type", "")
    res = event.get("resource", {})
    sub_id = res.get("id") or res.get("billing_agreement_id")
    status = res.get("status") or ""
    payer_id = (res.get("subscriber") or {}).get("payer_id") or (res.get("payer", {}) or {}).get("payer_info", {}).get("payer_id")
    plan_id = res.get("plan_id")

    from bot import get_db_conn, set_premium, set_subscription_record
    conn = get_db_conn()

    try:
        row = conn.execute("SELECT user_id FROM subscriptions WHERE subscription_id=?", (sub_id,)).fetchone()
        uid = row[0] if row else None
        set_subscription_record(sub_id, uid, status, payer_id, plan_id)
        log.info("webhook %s: sub=%s status=%s uid=%s plan=%s", et, sub_id, status, uid, plan_id)

        # Apply premium changes
        if et in ("BILLING.SUBSCRIPTION.ACTIVATED", "PAYMENT.SALE.COMPLETED", "BILLING.SUBSCRIPTION.RE-ACTIVATED"):
            if uid: set_premium(uid, True)
        elif et in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED", "BILLING.SUBSCRIPTION.SUSPENDED"):
            if uid: set_premium(uid, False)
    except Exception as e:
        log.error("webhook processing error: %s\n%s", e, traceback.format_exc())

    return "ok", 200

# ======== RUN LOOPS ========
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

    if ALERT_INTERVAL_SEC and ALERT_INTERVAL_SEC > 0:
        def _worker():
            while True:
                try:
                    from __main__ import run_alert_tick
                    n = run_alert_tick()
                    log.info("Alert worker tick: triggered=%d", n)
                except Exception as e:
                    log.error("Alert worker error: %s\n%s", e, traceback.format_exc())
                time.sleep(ALERT_INTERVAL_SEC)
        threading.Thread(target=_worker, daemon=True).start()

    try:
        run_flask()
    finally:
        TG_LOOP.call_soon_threadsafe(lambda: asyncio.create_task(tg_shutdown()))
        TG_LOOP.call_soon_threadsafe(TG_LOOP.stop)
