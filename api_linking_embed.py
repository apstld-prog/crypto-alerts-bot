# api_linking_embed.py
from __future__ import annotations
import os, secrets
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import text
from db import session_scope

API_KEY = os.getenv("API_KEY", "")

def require_api_key(x_api_key: str = Header(default="")):
    if not API_KEY:
        return True
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

def _ensure_app_links():
    with session_scope() as s:
        s.execute(text("""
        CREATE TABLE IF NOT EXISTS app_links(
            app_token  VARCHAR(64) PRIMARY KEY,
            telegram_id VARCHAR(64),
            fcm_token   VARCHAR(256),
            pin         VARCHAR(12),
            pin_expires TIMESTAMP,
            created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_app_links_tg ON app_links(telegram_id);
        CREATE INDEX IF NOT EXISTS idx_app_links_pin ON app_links(pin);
        """))

class StartLinkOut(BaseModel):
    app_token: str
    pin: str
    pin_expires_ts: int

class ConfirmIn(BaseModel):
    pin: str
    tg: str

def _new_pin() -> str:
    return f"{secrets.randbelow(1000000):06d}"

def _new_token() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(40))

def attach_linking_routes(app: FastAPI) -> None:
    _ensure_app_links()

    @app.post("/api/link/start", response_model=StartLinkOut, dependencies=[Depends(require_api_key)])
    def link_start():
        token = _new_token()
        pin = _new_pin()
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        with session_scope() as s:
            s.execute(text("""
                INSERT INTO app_links(app_token, pin, pin_expires)
                VALUES(:tok, :pin, :exp)
            """), {"tok": token, "pin": pin, "exp": expires})
        return StartLinkOut(app_token=token, pin=pin, pin_expires_ts=int(expires.timestamp()))

    @app.post("/api/link/confirm", dependencies=[Depends(require_api_key)])
    def link_confirm(body: ConfirmIn):
        now = datetime.now(timezone.utc)
        with session_scope() as s:
            row = s.execute(text("""
                SELECT app_token, pin_expires FROM app_links WHERE pin=:pin
            """), {"pin": body.pin}).first()
            if not row:
                raise HTTPException(404, detail="PIN not found")
            if row.pin_expires and row.pin_expires < now:
                raise HTTPException(410, detail="PIN expired")
            s.execute(text("""
                UPDATE app_links SET telegram_id=:tg, pin=NULL, pin_expires=NULL, updated_at=NOW()
                WHERE app_token=:tok
            """), {"tg": body.tg, "tok": row.app_token})
        return {"ok": True, "app_token": row.app_token}

    @app.get("/api/link/status", dependencies=[Depends(require_api_key)])
    def link_status(app_token: str = Query(...)):
        with session_scope() as s:
            row = s.execute(text("""
                SELECT app_token, telegram_id, fcm_token, pin, pin_expires FROM app_links WHERE app_token=:tok
            """), {"tok": app_token}).first()
        if not row:
            raise HTTPException(404, detail="Not found")
        return dict(row._mapping)

    @app.post("/api/link/fcm", dependencies=[Depends(require_api_key)])
    def link_save_fcm(app_token: str = Query(...), fcm_token: str = Query(...)):
        with session_scope() as s:
            s.execute(text("""
                UPDATE app_links SET fcm_token=:fcm, updated_at=NOW() WHERE app_token=:tok
            """), {"tok": app_token, "fcm": fcm_token})
        return {"ok": True}
