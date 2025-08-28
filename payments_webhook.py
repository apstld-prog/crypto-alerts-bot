#!/usr/bin/env python3
import os, logging, sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory

DB_PATH = os.getenv("DB_PATH", "bot.db")
VERIFY_WEBHOOK = os.getenv("VERIFY_WEBHOOK", "false").lower() == "true"
# For production, implement PayPal signature verification and set VERIFY_WEBHOOK=true.

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        premium_active INTEGER DEFAULT 0,
        premium_until TEXT
    )""")
    return conn

CONN = db()

def set_premium(user_id: int, days: int = 31):
    until = (datetime.utcnow() + timedelta(days=days)).isoformat()
    CONN.execute(
        "INSERT INTO users(user_id, premium_active, premium_until) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET premium_active=excluded.premium_active, premium_until=excluded.premium_until",
        (user_id, 1, until)
    )
    CONN.commit()

@app.route("/paypal/webhook", methods=["POST"])
def paypal_webhook():
    event = request.get_json(force=True, silent=True) or {}
    app.logger.info(f"Webhook event: {event.get('event_type')}")

    # ⚠️ TODO: Implement signature verification if VERIFY_WEBHOOK=true (recommended for production).

    resource = event.get("resource", {})
    custom_id = resource.get("custom_id") or resource.get("subscriber", {}).get("custom_id")

    if event.get("event_type") in (
        "BILLING.SUBSCRIPTION.ACTIVATED",
        "BILLING.SUBSCRIPTION.RE-ACTIVATED",
        "PAYMENT.SALE.COMPLETED",
        "PAYMENT.CAPTURE.COMPLETED"
    ):
        if custom_id:
            try:
                user_id = int(custom_id)
                set_premium(user_id, 31)
                app.logger.info(f"✅ Premium activated for user {user_id}")
            except Exception as e:
                app.logger.error(f"❌ Failed to activate premium: {e}")

    return jsonify({"ok": True})

@app.route("/subscribe.html")
def subscribe_page():
    return send_from_directory(".", "subscribe.html")

@app.route("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
