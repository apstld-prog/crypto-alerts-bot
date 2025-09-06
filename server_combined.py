# server_combined.py
import os, json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Iterable

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
import httpx

from db import session_scope, User, Subscription

app = FastAPI(title="crypto-alerts-web")

# ───────── ENV ─────────
def _env(name: str, default: str = "") -> str:
    # Trim spaces to avoid trailing-space bugs
    v = os.getenv(name, default)
    if isinstance(v, str):
        return v.strip()
    return v

PAYPAL_MODE = _env("PAYPAL_MODE", "live")  # live | sandbox
PAYPAL_CLIENT_ID = _env("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = _env("PAYPAL_SECRET", "")
PAYPAL_WEBHOOK_ID = _env("PAYPAL_WEBHOOK_ID", "")
ADMIN_KEY = _env("ADMIN_KEY", "")
WEB_URL = _env("WEB_URL", "")

# for admin notifications
BOT_TOKEN = _env("BOT_TOKEN", "")
_ADMIN_IDS: Iterable[str] = [s.strip() for s in (_env("ADMIN_TELEGRAM_IDS","")).split(",") if s.strip()]

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

# ───────── Utilities ─────────
def parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None

def _mask(v: Optional[str], keep: int = 4) -> Optional[str]:
    if not v:
        return v
    if len(v) <= keep:
        return "***"
    return v[:keep] + "…" + "***"

def send_admin_msg(text: str) -> None:
    if not BOT_TOKEN or not _ADMIN_IDS:
        return
    try:
        with httpx.Client(timeout=10.0) as c:
            for admin_id in _ADMIN_IDS:
                if not admin_id:
                    continue
                try:
                    c.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={"chat_id": admin_id, "text": text},
                    )
                except Exception:
                    pass
    except Exception:
        pass

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

# ───────── Basic ─────────
@app.get("/")
async def root():
    return PlainTextResponse("crypto-alerts-web up")

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

# ───────── DEBUG (for you) ─────────
@app.get("/debug/env")
async def debug_env(key: str = Query("")):
    if key != ADMIN_KEY or not ADMIN_KEY:
        raise HTTPException(status_code=403, detail="forbidden")
    data = {
        "PAYPAL_MODE": PAYPAL_MODE,
        "PAYPAL_CLIENT_ID": _mask(PAYPAL_CLIENT_ID, 6),
        "PAYPAL_SECRET": _mask(PAYPAL_SECRET, 6),
        "PAYPAL_WEBHOOK_ID": _mask(PAYPAL_WEBHOOK_ID, 6),
        "WEB_URL": WEB_URL,
        "BOT_TOKEN": _mask(BOT_TOKEN, 6),
        "ADMIN_TELEGRAM_IDS": list(_ADMIN_IDS),
    }
    return JSONResponse(data, status_code=200)

@app.get("/debug/paypal_token")
async def debug_paypal_token(key: str = Query("")):
    if key != ADMIN_KEY or not ADMIN_KEY:
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        token = await paypal_access_token()
        return JSONResponse({"ok": True, "token_prefix": token[:12] + "…"}, status_code=200)
    except httpx.HTTPStatusError as he:
        return JSONResponse({"ok": False, "error": "httpstatus", "status": he.response.status_code, "body": he.response.text[:400]}, status_code=200)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

# ───────── 1) Start: create subscription with custom_id ─────────
@app.get("/billing/paypal/start")
async def paypal_start(
    tg: str = Query(..., description="Telegram user id"),
    plan_id: str = Query(..., description="PayPal plan id, e.g. P-XXXX"),
):
    try:
        token = await paypal_access_token()
    except Exception:
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
    return PlainTextResponse(f"Thanks! If payment is approved, you'll be upgraded shortly. tg={tg}")

@app.get("/billing/paypal/cancelled")
async def paypal_cancelled(tg: Optional[str] = None):
    return PlainTextResponse(f"Subscription was cancelled before approval. tg={tg}")

# ───────── 2) Webhook ─────────
@app.post("/billing/paypal/webhook")
async def paypal_webhook(request: Request):
    if not PAYPAL_WEBHOOK_ID:
        return JSONResponse({"ok": True, "note": "no verification; missing PAYPAL_WEBHOOK_ID"}, status_code=200)

    hdr = request.headers
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", "ignore")
    try:
        event = json.loads(body_text or "{}")
    except Exception:
        event = {}

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
    plan_id = resource.get("plan_id") or (resource.get("plan_overridden") or {}).get("id")

    print({"paypal_webhook_ok": {"etype": etype, "sub": sub_id, "status": status, "custom_id": custom_id, "plan": plan_id}})

    if etype in ("BILLING.SUBSCRIPTION.ACTIVATED", "PAYMENT.SALE.COMPLETED", "PAYMENT.CAPTURE.COMPLETED"):
        cpe = parse_iso(((resource.get("billing_info") or {}).get("next_billing_time"))) \
              or (datetime.now(timezone.utc) + timedelta(days=30))
        if custom_id:
            set_premium_and_upsert_subscription(
                telegram_id=str(custom_id),
                provider_ref=sub_id or "",
                provider_status=status or "ACTIVE",
                period_end=cpe,
                status_internal="ACTIVE",
            )
            send_admin_msg(f"💎 New Premium activated\nTG: {custom_id}\nSub: {sub_id}\nStatus: {status}\nPlan: {plan_id}\nCPE: {cpe.isoformat()}")
        else:
            send_admin_msg(f"ℹ️ PayPal ACTIVATE without custom_id\nSub: {sub_id}\nStatus: {status}\nUse /claim {sub_id} from your admin account to bind.")
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
            send_admin_msg(f"⚠️ Subscription updated\nTG: {custom_id}\nSub: {sub_id}\nStatus: {status}\nCPE: {(cpe.isoformat() if cpe else '-')}")
        else:
            send_admin_msg(f"⚠️ Subscription {status} without custom_id\nSub: {sub_id}\n(legacy link?)")

    return JSONResponse({"ok": True}, status_code=200)

# ───────── 3) Admin claim ─────────
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
    send_admin_msg(f"✅ CLAIM done\nTG: {tg}\nSub: {subscription_id}\nStatus: {status}\nCPE: {cpe.isoformat()}")
    return JSONResponse({"ok": True, "status": status, "current_period_end": cpe.isoformat()}, status_code=200)

# ───────── 4) Admin cancel auto-renew ─────────
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
    send_admin_msg(f"🛑 Auto-renew cancelled\nTG: {telegram_id}\nSub: {sub.provider_ref}")
    return JSONResponse({"ok": True, "keeps_access_until": (sub.current_period_end.isoformat() if sub.current_period_end else None)}, status_code=200)
