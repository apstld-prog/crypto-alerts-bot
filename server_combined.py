# server_combined.py
import os, json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
import httpx

from db import session_scope, User, Subscription

app = FastAPI(title="crypto-alerts-web")

# ───────── ENV ─────────
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "live")  # live | sandbox
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
WEB_URL = os.getenv("WEB_URL", "")

def paypal_base() -> str:
    return "https://api-m.paypal.com" if PAYPAL_MODE == "live" else "https://api-m.sandbox.paypal.com"

async def paypal_access_token() -> str:
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise RuntimeError("PAYPAL_CLIENT_ID/SECRET not set")
    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.post(
            f"{paypal_base()}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
            data={"grant_type": "client_credentials"},
        )
        r.raise_for_status()
        return r.json()["access_token"]

# ───────── DB util ─────────
def parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None

def set_premium_and_upsert_subscription(
    telegram_id: str,
    provider_ref: str,
    provider_status: str,
    period_end: Optional[datetime],
    status_internal: Optional[str] = None,
):
    with session_scope() as session:
        user = session.query(User).filter(User.telegram_id == str(telegram_id)).one_or_none()
        if not user:
            user = User(telegram_id=str(telegram_id), is_premium=True)
            session.add(user)
            session.flush()
        else:
            user.is_premium = True

        sub = session.query(Subscription).filter(
            Subscription.provider == "paypal",
            Subscription.provider_ref == provider_ref,
        ).one_or_none()
        if not sub:
            sub = Subscription(
                user_id=user.id,
                provider="paypal",
                provider_ref=provider_ref,
                status_internal=status_internal or "ACTIVE",
                provider_status=provider_status,
                current_period_end=period_end,
            )
            session.add(sub)
        else:
            sub.user_id = user.id
            sub.provider_status = provider_status
            if status_internal:
                sub.status_internal = status_internal
            if period_end:
                sub.current_period_end = period_end

@app.get("/")
async def root():
    return PlainTextResponse("crypto-alerts-web up")

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

# ───────── Start Subscription ─────────
@app.get("/billing/paypal/start")
async def paypal_start(
    tg: str = Query(...),
    plan_id: str = Query(...),
):
    try:
        token = await paypal_access_token()
    except Exception as e:
        raise HTTPException(status_code=500, detail="paypal token error")

    return_url = f"{WEB_URL}/billing/paypal/success?tg={tg}"
    cancel_url = f"{WEB_URL}/billing/paypal/cancelled?tg={tg}"

    payload = {
        "plan_id": plan_id,
        "custom_id": str(tg),
        "application_context": {
            "brand_name": "Crypto Alerts",
            "locale": "en-US",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "SUBSCRIBE_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        }
    }

    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.post(
            f"{paypal_base()}/v1/billing/subscriptions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    r.raise_for_status()
    data = r.json()

    approval_url = None
    for link in data.get("links", []):
        if link.get("rel") == "approve":
            approval_url = link.get("href")
            break
    if not approval_url:
        raise HTTPException(status_code=500, detail="approval link not found")

    return RedirectResponse(url=approval_url, status_code=302)

@app.get("/billing/paypal/success")
async def paypal_success(tg: Optional[str] = None):
    return PlainTextResponse(f"Thanks! If payment is approved, you'll be upgraded soon. tg={tg}")

@app.get("/billing/paypal/cancelled")
async def paypal_cancelled(tg: Optional[str] = None):
    return PlainTextResponse(f"Subscription was cancelled before approval. tg={tg}")

# ───────── Webhook ─────────
@app.post("/billing/paypal/webhook")
async def paypal_webhook(request: Request):
    if not PAYPAL_WEBHOOK_ID:
        return JSONResponse({"ok": True, "note": "no verification; missing PAYPAL_WEBHOOK_ID"}, status_code=200)

    hdr = request.headers
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", "ignore")
    event = json.loads(body_text or "{}")

    # Verify
    token = await paypal_access_token()
    verify_payload = {
        "transmission_id": hdr.get("Paypal-Transmission-Id"),
        "transmission_time": hdr.get("Paypal-Transmission-Time"),
        "cert_url": hdr.get("Paypal-Cert-Url"),
        "auth_algo": hdr.get("Paypal-Auth-Algo"),
        "transmission_sig": hdr.get("Paypal-Transmission-Sig"),
        "webhook_id": PAYPAL_WEBHOOK_ID,
        "webhook_event": event,
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        vr = await client.post(
            f"{paypal_base()}/v1/notifications/verify-webhook-signature",
            headers={"Authorization": f"Bearer {token}"},
            json=verify_payload,
        )
    vr.raise_for_status()
    if vr.json().get("verification_status") != "SUCCESS":
        raise HTTPException(status_code=400, detail="bad signature")

    etype = event.get("event_type")
    resource = event.get("resource") or {}
    sub_id = resource.get("id") or resource.get("subscription_id")
    status = resource.get("status")
    custom_id = resource.get("custom_id")

    print({"paypal_webhook_ok": {"etype": etype, "sub": sub_id, "status": status, "custom_id": custom_id}})

    if etype in ("BILLING.SUBSCRIPTION.ACTIVATED", "PAYMENT.SALE.COMPLETED"):
        cpe = parse_iso(((resource.get("billing_info") or {}).get("next_billing_time")))
        if not cpe:
            cpe = datetime.now(timezone.utc) + timedelta(days=30)
        if custom_id:
            set_premium_and_upsert_subscription(
                telegram_id=str(custom_id),
                provider_ref=sub_id or "",
                provider_status=status or "ACTIVE",
                period_end=cpe,
                status_internal="ACTIVE",
            )
    elif etype in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED"):
        cpe = parse_iso(((resource.get("billing_info") or {}).get("next_billing_time")))
        if custom_id:
            set_premium_and_upsert_subscription(
                telegram_id=str(custom_id),
                provider_ref=sub_id or "",
                provider_status=status or "CANCELLED",
                period_end=cpe,
                status_internal="CANCELLED",
            )
    return JSONResponse({"ok": True}, status_code=200)

# ───────── Claim (Admin) ─────────
@app.post("/billing/paypal/claim")
async def paypal_claim(
    subscription_id: str = Query(...),
    tg: str = Query(...),
    key: str = Query(...),
):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="forbidden")

    token = await paypal_access_token()
    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.get(
            f"{paypal_base()}/v1/billing/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    r.raise_for_status()
    sub = r.json()
    status = sub.get("status") or "APPROVED"
    cpe = parse_iso(((sub.get("billing_info") or {}).get("next_billing_time"))) or (datetime.now(timezone.utc)+timedelta(days=30))

    set_premium_and_upsert_subscription(
        telegram_id=str(tg),
        provider_ref=subscription_id,
        provider_status=status,
        period_end=cpe,
        status_internal="ACTIVE" if status in ("ACTIVE","APPROVED") else status,
    )
    return JSONResponse({"ok": True, "status": status, "current_period_end": cpe.isoformat()}, status_code=200)

# ───────── Cancel auto-renew (Admin) ─────────
@app.post("/billing/paypal/cancel")
async def paypal_cancel_autorenew(
    telegram_id: str = Query(...),
    key: str = Query(...),
):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="forbidden")

    with session_scope() as session:
        user = session.query(User).filter(User.telegram_id == str(telegram_id)).one_or_none()
        if not user:
            return JSONResponse({"ok": False, "error": "user not found"}, status_code=404)
        sub = session.query(Subscription).filter(
            Subscription.user_id==user.id,
            Subscription.provider=="paypal"
        ).order_by(Subscription.id.desc()).first()
        if not sub or not sub.provider_ref:
            return JSONResponse({"ok": False, "error": "subscription not found"}, status_code=404)

    token = await paypal_access_token()
    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.post(
            f"{paypal_base()}/v1/billing/subscriptions/{sub.provider_ref}/cancel",
            headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"},
            json={"reason":"user requested cancel"},
        )
    if r.status_code not in (200,204):
        return JSONResponse({"ok": False, "error": r.text}, status_code=400)
    return JSONResponse({"ok": True, "keeps_access_until": (sub.current_period_end.isoformat() if sub.current_period_end else None)}, status_code=200)
