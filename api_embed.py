# api_embed.py
from __future__ import annotations
import os, time
from typing import Optional, Literal
from fastapi import FastAPI, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import text
from db import session_scope
from plans import build_plan_info, plan_status_line
import features_market as market

API_KEY = os.getenv("API_KEY", "")

def _admin_ids() -> set[str]:
    raw = os.getenv("ADMIN_TELEGRAM_IDS", "") or ""
    return {x.strip() for x in raw.split(",") if x.strip()}

def require_api_key(x_api_key: str = Header(default="")):
    if not API_KEY:
        return True
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

class LinkIn(BaseModel):
    tg: str
    app_token: Optional[str] = None
    fcm_token: Optional[str] = None

class AlertIn(BaseModel):
    tg: str
    symbol: str
    rule: Literal["price_above","price_below"]
    value: float

class AlertToggle(BaseModel):
    enabled: bool

class VerifyIn(BaseModel):
    tg: str
    productId: str
    purchaseToken: str

def _ensure_app_links():
    with session_scope() as s:
        s.execute(text("""
        CREATE TABLE IF NOT EXISTS app_links(
            telegram_id VARCHAR(64) PRIMARY KEY,
            app_token VARCHAR(128),
            fcm_token VARCHAR(256),
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """))

def attach_api_routes(app: FastAPI) -> None:
    @app.get("/api/app-config", dependencies=[Depends(require_api_key)])
    def app_config():
        return {
            "version": 1,
            "min_app_build": 1,
            "feature_flags": {"advisor_enabled": True, "pump_live_enabled": True},
            "tabs": [
                {"key": "market", "title": "Market", "visible": True},
                {"key": "alerts", "title": "Alerts", "visible": True},
                {"key": "news", "title": "News", "visible": True},
                {"key": "advisor", "title": "Advisor", "visible": True, "premium_only": True},
                {"key": "settings", "title": "Settings", "visible": True},
            ],
            "sections": {
                "market": [
                    {"type": "stat", "title": "Fear & Greed", "endpoint": "/api/feargreed"},
                    {"type": "list", "title": "Top Gainers", "endpoint": "/api/topmovers?dir=gainers", "limit": 10},
                    {"type": "list", "title": "Top Losers", "endpoint": "/api/topmovers?dir=losers", "limit": 10},
                ],
                "news": [{"type": "news_list", "endpoint": "/api/news?symbol=BTC&limit=15"}],
                "advisor": [{"type": "server_view", "endpoint": "/api/advisor/get"}]
            }
        }

    @app.post("/api/link", dependencies=[Depends(require_api_key)])
    def link(inb: LinkIn):
        _ensure_app_links()
        with session_scope() as s:
            s.execute(text("""
                INSERT INTO app_links(telegram_id, app_token, fcm_token)
                VALUES(:tg, :tok, :fcm)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    app_token=EXCLUDED.app_token,
                    fcm_token=EXCLUDED.fcm_token,
                    updated_at=NOW()
            """), {"tg": inb.tg, "tok": inb.app_token or "", "fcm": inb.fcm_token or ""})
        return {"ok": True}

    @app.get("/api/plan", dependencies=[Depends(require_api_key)])
    def plan(tg: str = Query(...)):
        info = build_plan_info(tg, _admin_ids())
        return {"premium": info.has_unlimited, "used": info.alerts_count, "limit": info.free_limit, "status": plan_status_line(info), "user_id": info.user_id}

    @app.get("/api/alerts", dependencies=[Depends(require_api_key)])
    def alerts_list(tg: str = Query(...)):
        with session_scope() as s:
            row = s.execute(text("SELECT id FROM users WHERE telegram_id=:tg"), {"tg": tg}).first()
            if not row:
                return []
            uid = int(row.id)
            rows = s.execute(text("""
                SELECT id, symbol, rule, value, enabled, created_at, updated_at
                FROM alerts WHERE user_id=:uid ORDER BY created_at DESC
            """), {"uid": uid}).fetchall()
            return [dict(r._mapping) for r in rows]

    @app.post("/api/alerts", dependencies=[Depends(require_api_key)])
    def alerts_create(inb: AlertIn):
        with session_scope() as s:
            info = build_plan_info(inb.tg, _admin_ids())
            if not info.has_unlimited and info.alerts_count >= info.free_limit:
                raise HTTPException(402, detail="Free plan limit reached. Upgrade for unlimited alerts.")
            uid = s.execute(text("""
                INSERT INTO users(telegram_id, is_premium) VALUES(:tg, false)
                ON CONFLICT (telegram_id) DO NOTHING
                RETURNING id
            """), {"tg": inb.tg}).fetchone()
            if uid:
                user_id = int(uid.id)
            else:
                user_id = int(s.execute(text("SELECT id FROM users WHERE telegram_id=:tg"), {"tg": inb.tg}).scalar())
            s.execute(text("""
                INSERT INTO alerts(user_id, symbol, rule, value, enabled)
                VALUES(:uid, :symbol, :rule, :value, true)
            """), {"uid": user_id, "symbol": inb.symbol.upper(), "rule": inb.rule, "value": inb.value})
        return {"ok": True}

    @app.patch("/api/alerts/{alert_id}", dependencies=[Depends(require_api_key)])
    def alerts_toggle(alert_id: int, body: AlertToggle):
        with session_scope() as s:
            s.execute(text("UPDATE alerts SET enabled=:en, updated_at=NOW() WHERE id=:id"), {"en": body.enabled, "id": alert_id})
        return {"ok": True}

    @app.delete("/api/alerts/{alert_id}", dependencies=[Depends(require_api_key)])
    def alerts_delete(alert_id: int):
        with session_scope() as s:
            s.execute(text("DELETE FROM alerts WHERE id=:id"), {"id": alert_id})
        return {"ok": True}

    @app.get("/api/feargreed", dependencies=[Depends(require_api_key)])
    def feargreed():
        return market.get_fear_greed()

    @app.get("/api/funding", dependencies=[Depends(require_api_key)])
    def funding():
        return market.get_funding()

    @app.get("/api/topmovers", dependencies=[Depends(require_api_key)])
    def topmovers(dir: Literal["gainers","losers"] = Query("gainers")):
        return market.get_top_movers(direction=dir)

    @app.get("/api/news", dependencies=[Depends(require_api_key)])
    def news(symbol: Optional[str] = Query(None), limit: int = Query(15)):
        return market.get_crypto_news(symbol or "", limit)

    @app.get("/api/health", dependencies=[Depends(require_api_key)])
    def health_api():
        return {"ok": True, "ts": int(time.time())}

    @app.post("/api/billing/google/verify", dependencies=[Depends(require_api_key)])
    def verify_purchase(inb: VerifyIn):
        with session_scope() as s:
            s.execute(text("""
                INSERT INTO users(telegram_id, is_premium) VALUES(:tg, true)
                ON CONFLICT (telegram_id) DO UPDATE SET is_premium=true, updated_at=NOW()
            """), {"tg": inb.tg})
            s.execute(text("""
                INSERT INTO subscriptions(user_id, provider, provider_sub_id, status_internal)
                SELECT id, 'google', :subid, 'ACTIVE' FROM users WHERE telegram_id=:tg
            """), {"tg": inb.tg, "subid": inb.purchaseToken[:120]})
        return {"premium": True}
