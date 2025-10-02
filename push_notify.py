# push_notify.py
import os
from typing import Optional, Dict
from sqlalchemy import text
from db import session_scope

import firebase_admin
from firebase_admin import credentials, messaging

_initialized = False

def _ensure_init():
    global _initialized
    if _initialized:
        return
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not cred_path or not os.path.exists(cred_path):
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS δεν έχει οριστεί ή δεν υπάρχει το αρχείο.")
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    _initialized = True

def send_push_to_token(token: str, title: str, body: str, data: Optional[Dict[str,str]] = None) -> str:
    _ensure_init()
    msg = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data=data or {},
        token=token
    )
    resp = messaging.send(msg, dry_run=False)
    return resp

def send_push_to_tg(tg: str, title: str, body: str, data: Optional[Dict[str,str]] = None) -> Optional[str]:
    with session_scope() as s:
        row = s.execute(text("SELECT fcm_token FROM app_links WHERE telegram_id=:tg"), {"tg": tg}).first()
        if not row or not row.fcm_token:
            return None
        return send_push_to_token(row.fcm_token, title, body, data)
