import os
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from db import init_db, session_scope
from worker_logic import run_alert_cycle
from dotenv import load_dotenv

# Load .env locally if present
load_dotenv(override=False)

app = FastAPI(title="Crypto Alerts API")

ALERTS_SECRET = os.getenv("ALERTS_SECRET")
STRIPE_SECRET = os.getenv("STRIPE_SECRET")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

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


@app.get("/cron")
def cron(request: Request):
    # Simple shared-secret protection
    key = request.query_params.get("key")
    if not ALERTS_SECRET or key != ALERTS_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    with session_scope() as session:
        counters = run_alert_cycle(session)
    return {"ok": True, "counters": counters}


@app.get("/stats")
def stats():
    from sqlalchemy import select, func
    from db import User, Subscription, Alert

    with session_scope() as session:
        users = session.execute(select(func.count()).select_from(User)).scalar_one()
        premium = session.execute(select(func.count()).select_from(User).where(User.is_premium == True)).scalar_one()  # noqa: E712
        active_alerts = session.execute(
            text("""
            SELECT COUNT(*) FROM alerts
            WHERE enabled = 1
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """ )
        ).scalar_one()
        active_subs = session.execute(
            text("""
            SELECT COUNT(*) FROM subscriptions
            WHERE status_internal IN ('ACTIVE')
            """ )
        ).scalar_one()
    return {
        "users": users,
        "premium_users": premium,
        "active_alerts": active_alerts,
        "active_subscriptions": active_subs,
    }


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        # If you don't use Stripe, disable this route or set the secret
        raise HTTPException(status_code=400, detail="Stripe webhook not configured")
    import stripe
    stripe.api_key = STRIPE_SECRET

    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Idempotency guard example (implement your own persistence)
    # event_id = event.get("id")
    # if already_processed(event_id): return {"ok": True}

    # Minimal handler
    etype = event["type"]
    data = event["data"]["object"]

    # TODO: map statuses and upsert subscription + recompute user premium flag
    print({"msg": "stripe_event", "type": etype, "id": event.get("id")})
    return {"ok": True}
