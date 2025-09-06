# server_combined.py
import os
import json
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx

app = FastAPI(title="crypto-alerts-web")

# ───────── ENV ─────────
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "live")  # live | sandbox
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")

# Helper: PayPal base URL
def paypal_base() -> str:
    return "https://api-m.paypal.com" if PAYPAL_MODE == "live" else "https://api-m.sandbox.paypal.com"

async def paypal_access_token() -> str:
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise RuntimeError("PAYPAL_CLIENT_ID/SECRET not set")
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            f"{paypal_base()}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
            data={"grant_type": "client_credentials"},
        )
        r.raise_for_status()
        data = r.json()
        return data["access_token"]

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

# ───────── Webhook ─────────
@app.post("/billing/paypal/webhook")
async def paypal_webhook(request: Request):
    """
    Verifies the webhook with PayPal and returns 200.
    Logs key info so βλέπεις στα Render logs τι ήρθε.
    """
    if not PAYPAL_WEBHOOK_ID:
        # Δέξου αλλά προειδοποίησε ότι δεν μπορεί να γίνει verify
        body = await request.body()
        print({"paypal_webhook_warn": "PAYPAL_WEBHOOK_ID missing", "raw": body[:500].decode("utf-8", "ignore")})
        return JSONResponse({"ok": True, "note": "no verification; missing PAYPAL_WEBHOOK_ID"}, status_code=200)

    # Headers που χρειάζονται για verify
    hdr = request.headers
    transmission_id = hdr.get("Paypal-Transmission-Id")
    transmission_time = hdr.get("Paypal-Transmission-Time")
    cert_url = hdr.get("Paypal-Cert-Url")
    auth_algo = hdr.get("Paypal-Auth-Algo")
    transmission_sig = hdr.get("Paypal-Transmission-Sig")

    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", "ignore")

    # Κάνε verify στο PayPal
    try:
        token = await paypal_access_token()
        verify_payload = {
            "transmission_id": transmission_id,
            "transmission_time": transmission_time,
            "cert_url": cert_url,
            "auth_algo": auth_algo,
            "transmission_sig": transmission_sig,
            "webhook_id": PAYPAL_WEBHOOK_ID,
            "webhook_event": json.loads(body_text or "{}"),
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            vr = await client.post(
                f"{paypal_base()}/v1/notifications/verify-webhook-signature",
                headers={"Authorization": f"Bearer {token}"},
                json=verify_payload,
            )
            vr.raise_for_status()
            vdata = vr.json()
    except Exception as e:
        print({"paypal_webhook_verify_error": str(e)})
        raise HTTPException(status_code=400, detail="verification failed")

    if (vdata or {}).get("verification_status") != "SUCCESS":
        print({"paypal_webhook_verify_failed": vdata})
        raise HTTPException(status_code=400, detail="bad signature")

    # Επαλήθευση OK – επεξεργασία event
    event = json.loads(body_text or "{}")
    etype = event.get("event_type")
    resource = event.get("resource") or {}

    # Χρήσιμο log για debug
    key_log: Dict[str, Any] = {
        "etype": etype,
        "id": event.get("id"),
        "create_time": event.get("create_time"),
        "summary": event.get("summary"),
        "subscription_id": resource.get("id") or resource.get("subscription_id"),
        "status": resource.get("status"),
        "custom_id": resource.get("custom_id"),
        "plan_id": (resource.get("plan_id") or (resource.get("plan_overridden") or {}).get("id")),
        "payer_email": (resource.get("subscriber") or {}).get("email_address"),
    }
    print({"paypal_webhook_ok": key_log})

    # TODO: mapping σε χρήστη Telegram
    # Αν χρησιμοποιήσουμε ροή /billing/paypal/start με custom_id=<telegram_id>,
    # εδώ παίρνουμε resource.custom_id και:
    #  - βρίσκουμε user by telegram_id
    #  - users.is_premium = TRUE
    #  - εισάγουμε/ενημερώνουμε subscriptions
    # Προς το παρόν απλώς επιστρέφουμε 200.

    return JSONResponse({"ok": True}, status_code=200)

# Προαιρετικό: admin test για να δεις ότι ο web server “ζει”
@app.get("/")
async def root():
    return PlainTextResponse("crypto-alerts-web up")
