import os
import json
from typing import Optional, Tuple

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text, select, func
from dotenv import load_dotenv
import requests

from db import init_db, session_scope, User, Subscription, Alert
from worker_logic import run_alert_cycle

# Load local .env if exists (useful for dev)
load_dotenv(override=False)

app = FastAPI(title="Crypto Alerts API")

# Secrets & configs from env
ALERTS_SECRET = os.getenv("ALERTS_SECRET")
ADMIN_KEY = os.getenv("ADMIN_KEY")

# ---- PayPal config
PAYPAL_ENV = (os.getenv("PAYPAL_ENV") or "sandbox").lower()  # sandbox | live
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID")  # from Dashboard → Webhooks

PAYPAL_API = "https://api-m.paypal.com" if PAYPAL_ENV == "live" else "https://api-m.sandbox.paypal.com"


@app.on_event("startup")
def on_startup():
    """Init DB on startup"""
    init_db()


@app.get("/")
def root():
    return {"ok": True, "service": "crypto-alerts", "payments": "paypal", "env": PAYPAL_ENV}


@app.get("/healthz")
def healthz():
    """Health check — tests DB connectivity"""
    from db import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/stats")
def stats():
    """Show global stats (users, alerts, subs)"""
    with session_scope() as session:
        users = session.execute(select(func.count()).select_from(User)).scalar_one()
        premium = session.execute(
            select(func.count()).select_from(User).where(User.is_premium == True)  # noqa
        ).scalar_one()
        active_alerts = session.execute(
            text(
                """
                SELECT COUNT(*) FROM alerts
                WHERE enabled = 1
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """
            )
        ).scalar_one()
        active_subs = session.execute(
            text(
                """
                SELECT COUNT(*) FROM subscriptions
                WHERE status_internal IN ('ACTIVE')
                """
            )
        ).scalar_one()
    return {
        "users": users,
        "premium_users": premium,
        "active_alerts": active_alerts,
        "active_subscriptions": active_subs,
    }


@app.get("/cron")
def cron(request: Request):
    """Manual/cron trigger for alerts"""
    key = request.query_params.get("key")
    if not ALERTS_SECRET or key != ALERTS_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    with session_scope() as session:
        counters = run_alert_cycle(session)
    return {"ok": True, "counters": counters}


# =============================================================================
#                               PayPal Webhooks
# =============================================================================

def _paypal_get_token() -> str:
    """Get OAuth2 access token from PayPal."""
    if not (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET):
        raise HTTPException(status_code=400, detail="PayPal not configured (missing client id/secret)")
    try:
        r = requests.post(
            f"{PAYPAL_API}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["access_token"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"paypal_oauth_error: {e}")


def _paypal_verify_webhook(headers: dict, body: dict) -> bool:
    """Verify PayPal webhook signature per docs using /v1/notifications/verify-webhook-signature."""
    if not PAYPAL_WEBHOOK_ID:
        # You can still accept (NOT recommended), but here we enforce presence.
        raise HTTPException(status_code=400, detail="PayPal not configured (missing PAYPAL_WEBHOOK_ID)")

    transmission_id = headers.get("paypal-transmission-id")
    transmission_time = headers.get("paypal-transmission-time")
    cert_url = headers.get("paypal-cert-url")
    auth_algo = headers.get("paypal-auth-algo")
    transmission_sig = headers.get("paypal-transmission-sig")

    if not all([transmission_id, transmission_time, cert_url, auth_algo, transmission_sig]):
        raise HTTPException(status_code=400, detail="Missing PayPal verification headers")

    token = _paypal_get_token()

    try:
        vr = requests.post(
            f"{PAYPAL_API}/v1/notifications/verify-webhook-signature",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={
                "auth_algo": auth_algo,
                "cert_url": cert_url,
                "transmission_id": transmission_id,
                "transmission_sig": transmission_sig,
                "transmission_time": transmission_time,
                "webhook_id": PAYPAL_WEBHOOK_ID,
                "webhook_event": body,
            },
            timeout=20,
        )
        vr.raise_for_status()
        status = vr.json().get("verification_status")
        return status == "SUCCESS" or status == "VERIFIED"  # docs use "SUCCESS"
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"paypal_verify_error: {e}")


def _attach_subscription_to_user_and_upsert(session, provider_status: str, status_internal: str,
                                            custom_id: Optional[str]) -> Tuple[Optional[int], bool]:
    """
    Link subscription to a user via custom_id (we expect you pass telegram_id as custom_id during checkout).
    If user found: set is_premium for ACTIVE, insert a Subscription row (provider='paypal').
    Returns (user_id, created_new_row:boolean).
    """
    user_id = None
    created = False

    if custom_id:
        # Try to find user by telegram_id == custom_id
        user = session.execute(select(User).where(User.telegram_id == str(custom_id))).scalar_one_or_none()
        if user:
            user_id = user.id
            # Update premium flag
            user.is_premium = (status_internal == "ACTIVE")
            session.add(user)

    # Insert a new subscription row (simple history). We don't store PayPal sub ID to avoid schema changes.
    sub = Subscription(
        user_id=user_id,
        provider="paypal",
        provider_status=provider_status,  # e.g., ACTIVE, CANCELLED
        status_internal=status_internal,  # ACTIVE | CANCELLED
    )
    session.add(sub)
    created = True
    session.flush()

    return user_id, created


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    """
    Handle PayPal webhooks.

    IMPORTANT:
    - During your PayPal subscription checkout flow you should pass a `custom_id` that equals the user's Telegram ID.
      Then webhooks will allow us to map the payer to a User and flip is_premium.
    """
    raw_body = await request.body()
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Verify signature
    if not _paypal_verify_webhook(request.headers, body):
        raise HTTPException(status_code=400, detail="Invalid PayPal signature")

    event_type = body.get("event_type")
    resource = body.get("resource", {}) or {}

    # Common fields
    status = resource.get("status")  # e.g., ACTIVE, CANCELLED
    custom_id = resource.get("custom_id")  # we expect telegram_id here (optional but recommended)

    handled = False
    with session_scope() as session:
        # Map key events: ACTIVATED / CANCELLED / EXPIRED / PAYMENT.CAPTURE.COMPLETED
        if event_type in ("BILLING.SUBSCRIPTION.ACTIVATED", "PAYMENT.SALE.COMPLETED", "PAYMENT.CAPTURE.COMPLETED"):
            _attach_subscription_to_user_and_upsert(session, provider_status=status or "ACTIVE", status_internal="ACTIVE", custom_id=custom_id)
            handled = True
        elif event_type in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.EXPIRED"):
            _attach_subscription_to_user_and_upsert(session, provider_status=status or "CANCELLED", status_internal="CANCELLED", custom_id=custom_id)
            handled = True

    return {"ok": True, "handled": handled, "event_type": event_type, "status": status, "custom_id": custom_id}


# ============ Admin (protected) ============
def require_admin(request: Request) -> bool:
    key = request.headers.get("x-admin-key") or request.query_params.get("key")
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True


@app.get("/admin/users")
def admin_users(_: bool = Depends(require_admin)):
    with session_scope() as session:
        rows = session.execute(
            select(User.id, User.telegram_id, User.is_premium, User.created_at)
            .order_by(User.id.desc())
        ).all()
    return [
        {
            "id": r.id,
            "telegram_id": r.telegram_id,
            "is_premium": bool(r.is_premium),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@app.get("/admin/alerts")
def admin_alerts(_: bool = Depends(require_admin)):
    with session_scope() as session:
        rows = session.execute(
            select(
                Alert.id,
                Alert.user_id,
                Alert.enabled,
                Alert.symbol,
                Alert.rule,
                Alert.value,
                Alert.cooldown_seconds,
                Alert.last_fired_at,
                Alert.expires_at,
                Alert.created_at,
            ).order_by(Alert.id.desc())
        ).all()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "enabled": bool(r.enabled),
            "symbol": r.symbol,
            "rule": r.rule,
            "value": r.value,
            "cooldown_seconds": r.cooldown_seconds,
            "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@app.get("/admin/subscriptions")
def admin_subscriptions(_: bool = Depends(require_admin)):
    with session_scope() as session:
        rows = session.execute(
            select(
                Subscription.id,
                Subscription.user_id,
                Subscription.provider,
                Subscription.provider_status,
                Subscription.status_internal,
                Subscription.current_period_end,
                Subscription.created_at,
            ).order_by(Subscription.id.desc())
        ).all()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "provider": r.provider,
            "provider_status": r.provider_status,
            "status_internal": r.status_internal,
            "current_period_end": r.current_period_end.isoformat() if r.current_period_end else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
