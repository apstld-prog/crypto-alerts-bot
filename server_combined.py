import os
import json
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text, select, func
from dotenv import load_dotenv

from db import init_db, session_scope, User, Subscription, Alert
from worker_logic import run_alert_cycle

# Load .env locally if present (no effect on Render unless you add a .env)
load_dotenv(override=False)

app = FastAPI(title="Crypto Alerts API")

ALERTS_SECRET = os.getenv("ALERTS_SECRET")
STRIPE_SECRET = os.getenv("STRIPE_SECRET")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
ADMIN_KEY = os.getenv("ADMIN_KEY")  # Admin API protection


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def root():
    return {"ok": True, "service": "crypto-alerts"}


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
            select(func.count()).select_from(User).where(User.is_premium == True)  # noqa: E712
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
    key = request.query_params.get("key")
    if not ALERTS_SECRET or key != ALERTS_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    with session_scope() as session:
        counters = run_alert_cycle(session)
    return {"ok": True, "counters": counters}


# ---------- Telegram webhook (optional if you use polling bot.py) ----------
# Example route if you ever switch to Telegram webhooks.
# @app.post("/telegram/webhook")
# async def telegram_webhook(request: Request):
#     body = await request.body()
#     print({"msg": "telegram_webhook", "len": len(body)})
#     return {"ok": True}


# ---------- Stripe Webhook (optional) ----------
@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=400, detail="Stripe webhook not configured")
    import stripe

    stripe.api_key = STRIPE_SECRET

    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    etype = event["type"]
    data = event["data"]["object"]
    # TODO: map statuses and upsert Subscription + recompute User.is_premium
    print({"msg": "stripe_event", "type": etype, "id": event.get("id")})
    return {"ok": True}


# ==================== Admin (read-only) ====================
def require_admin(request: Request) -> bool:
    key = request.headers.get("x-admin-key") or request.query_params.get("key")
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True


@app.get("/admin/users")
def admin_users(_: bool = Depends(require_admin)):
    with session_scope() as session:
        rows = session.execute(
            select(User.id, User.telegram_id, User.is_premium, User.created_at).order_by(User.id.desc())
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
