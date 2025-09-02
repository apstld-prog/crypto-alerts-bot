
import os
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text, select, func, desc
from dotenv import load_dotenv
import requests

from db import init_db, session_scope, User, Subscription, Alert
from worker_logic import run_alert_cycle

load_dotenv(override=False)

app = FastAPI(title="Crypto Alerts API")

ALERTS_SECRET = os.getenv("ALERTS_SECRET")
ADMIN_KEY = os.getenv("ADMIN_KEY")

# PayPal config
PAYPAL_ENV = (os.getenv("PAYPAL_ENV") or "sandbox").lower()  # sandbox | live
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID")
PAYPAL_API = "https://api-m.paypal.com" if PAYPAL_ENV == "live" else "https://api-m.sandbox.paypal.com"


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def root():
    return {"ok": True, "service": "crypto-alerts", "payments": "paypal", "env": PAYPAL_ENV}


@app.get("/healthz")
def healthz():
    from db import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/stats")
def stats():
    with session_scope() as session:
        users = session.execute(select(func.count()).select_from(User)).scalar_one()
        premium = session.execute(
            select(func.count()).select_from(User).where(User.is_premium == True)  # noqa
        ).scalar_one()
        active_alerts = session.execute(
            text("""                SELECT COUNT(*) FROM alerts
                WHERE enabled = 1
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """ )
        ).scalar_one()
        active_subs = session.execute(
            text("""                SELECT COUNT(*) FROM subscriptions
                WHERE status_internal IN ('ACTIVE','CANCEL_AT_PERIOD_END')
            """ )
        ).scalar_one()
    return {
        "users": users,
        "premium_users": premium,
        "active_alerts": active_alerts,
        "active_subscriptions": active_subs,
    }


@app.get("/cron")
def cron(request: Request):
    key = request.query_params.get("key")
    if not ALERTS_SECRET or key != ALERTS_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    with session_scope() as session:
        counters = run_alert_cycle(session)
    return {"ok": True, "counters": counters}


# ---------- PayPal helpers ----------
def _paypal_get_token() -> str:
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
    if not PAYPAL_WEBHOOK_ID:
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
        return status in ("SUCCESS", "VERIFIED")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"paypal_verify_error: {e}")


def _parse_next_billing(resource: dict) -> Optional[datetime]:
    try:
        info = resource.get("billing_info") or {}
        nxt = info.get("next_billing_time")
        if nxt:
            if nxt.endswith("Z"):
                return datetime.fromisoformat(nxt.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
            return datetime.fromisoformat(nxt)
    except Exception:
        pass
    return None


def _attach_or_update_subscription(session, *, telegram_id: Optional[str], provider_status: str,
                                   status_internal: str, provider_ref: Optional[str],
                                   current_period_end: Optional[datetime]):
    user = None
    if telegram_id:
        user = session.execute(select(User).where(User.telegram_id == str(telegram_id))).scalar_one_or_none()
        if user:
            user.is_premium = status_internal in ("ACTIVE", "CANCEL_AT_PERIOD_END")
            session.add(user)

    sub = Subscription(
        user_id=user.id if user else None,
        provider="paypal",
        provider_status=provider_status,
        status_internal=status_internal,
        provider_ref=provider_ref,
        current_period_end=current_period_end,
    )
    session.add(sub)
    session.flush()
    return sub.id


def _latest_subscription_for_user(session, user_id: int) -> Optional[Subscription]:
    return session.execute(
        select(Subscription).where(Subscription.user_id == user_id).order_by(desc(Subscription.id))
    ).scalar_one_or_none()


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    raw_body = await request.body()
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not _paypal_verify_webhook(request.headers, body):
        raise HTTPException(status_code=400, detail="Invalid PayPal signature")

    event_type = body.get("event_type")
    resource = body.get("resource", {}) or {}
    status = resource.get("status") or ""
    custom_id = resource.get("custom_id")
    provider_ref = resource.get("id")
    next_billing = _parse_next_billing(resource)

    with session_scope() as session:
        if event_type in ("BILLING.SUBSCRIPTION.ACTIVATED", "PAYMENT.SALE.COMPLETED", "PAYMENT.CAPTURE.COMPLETED"):
            _attach_or_update_subscription(
                session,
                telegram_id=custom_id,
                provider_status=status or "ACTIVE",
                status_internal="ACTIVE",
                provider_ref=provider_ref,
                current_period_end=next_billing,
            )
        elif event_type in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED"):
            _attach_or_update_subscription(
                session,
                telegram_id=custom_id,
                provider_status=status or "CANCELLED",
                status_internal="CANCEL_AT_PERIOD_END",
                provider_ref=provider_ref,
                current_period_end=next_billing,
            )
        elif event_type in ("BILLING.SUBSCRIPTION.EXPIRED",):
            _attach_or_update_subscription(
                session,
                telegram_id=custom_id,
                provider_status=status or "EXPIRED",
                status_internal="CANCELLED",
                provider_ref=provider_ref,
                current_period_end=next_billing,
            )

    return {"ok": True, "handled": True, "event_type": event_type}


def require_admin(request: Request) -> bool:
    key = request.headers.get("x-admin-key") or request.query_params.get("key")
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True


def _paypal_cancel_subscription(sub_id: str, reason: str = "user_requested") -> bool:
    token = _paypal_get_token()
    try:
        r = requests.post(
            f"{PAYPAL_API}/v1/billing/subscriptions/{sub_id}/cancel",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"reason": reason},
            timeout=15,
        )
        return r.status_code in (200, 204)
    except Exception:
        return False


@app.post("/billing/paypal/cancel")
async def cancel_autorenew(request: Request):
    require_admin(request)
    telegram_id = request.query_params.get("telegram_id")
    if not telegram_id:
        raise HTTPException(status_code=400, detail="telegram_id required")

    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id == str(telegram_id))).scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="user_not_found")

        sub = _latest_subscription_for_user(session, user.id)
        if not sub or not sub.provider_ref:
            raise HTTPException(status_code=404, detail="subscription_not_found")

        ok = _paypal_cancel_subscription(sub.provider_ref)
        if not ok:
            raise HTTPException(status_code=502, detail="paypal_cancel_failed")

        sub2 = Subscription(
            user_id=user.id,
            provider="paypal",
            provider_status="CANCELLED",
            status_internal="CANCEL_AT_PERIOD_END",
            provider_ref=sub.provider_ref,
            current_period_end=sub.current_period_end,
        )
        session.add(sub2)

    return {"ok": True, "message": "auto_renew_cancelled"}
