#!/usr/bin/env python3
import os
import threading
import logging

# --- Try to import your full payments app if it exists ---
flask_app = None
try:
    # If you have payments_webhook.py with: app = Flask(__name__)
    from payments_webhook import app as flask_app  # type: ignore
    logging.info("Using payments_webhook.app for web server.")
except Exception as e:
    logging.warning("payments_webhook import failed (%s). Falling back to minimal web app.", e)
    # Minimal fallback Flask app (health + static subscribe pages)
    from flask import Flask, send_from_directory, Response

    flask_app = Flask(__name__)

    @flask_app.after_request
    def _headers(resp: Response):
        # Safe defaults; loosen if you host PayPal buttons here
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://www.paypal.com; "
            "frame-src https://www.paypal.com; "
            "img-src 'self' data: https://www.paypalobjects.com; "
            "connect-src 'self' https://www.paypal.com https://api-m.paypal.com https://api-m.sandbox.paypal.com; "
            "style-src 'self' 'unsafe-inline'; "
            "form-action 'self' https://www.paypal.com"
        )
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        return resp

    @flask_app.get("/health")
    @flask_app.get("/healthz")
    def healthz():
        return "ok", 200

    @flask_app.get("/subscribe.html")
    def subscribe_live():
        # Will serve ./subscribe.html if it exists in the project directory
        if os.path.isfile("subscribe.html"):
            return send_from_directory(".", "subscribe.html")
        return ("<h3>Subscribe (LIVE)</h3><p>Place a subscribe.html file in project root.</p>", 200)

    @flask_app.get("/subscribe-sandbox.html")
    def subscribe_sandbox():
        if os.path.isfile("subscribe-sandbox.html"):
            return send_from_directory(".", "subscribe-sandbox.html")
        return ("<h3>Subscribe (SANDBOX)</h3><p>Place a subscribe-sandbox.html file in project root.</p>", 200)

# --- Serve Flask with waitress (production WSGI) ---
def run_flask():
    from waitress import serve  # lazy import to avoid dependency issues if unused
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    logging.info("Starting Flask on %s:%s", host, port)
    serve(flask_app, host=host, port=port)

# --- Import & run the Telegram bot ---
def run_bot():
    # Your bot.py must define run_bot()
    from bot import run_bot as _run_bot  # local import so logs show clearer errors
    _run_bot()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Start Flask server in a background thread
    t = threading.Thread(target=run_flask, daemon=True, name="flask-thread")
    t.start()

    # Start the Telegram bot (blocking)
    run_bot()
