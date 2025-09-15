# models_extras.py
# Lightweight helpers & tables that extend the core DB models.
# Provides a generic user_settings key/value store and simple accessors
# keyed by Telegram ID. Safe for a single-service deployment.

from __future__ import annotations

import os
from typing import Optional
from sqlalchemy import text
from db import session_scope, engine

# --- Schema bootstrap --------------------------------------------------------

_USER_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS user_settings (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, key)
);
"""

_CREATE_USER_BY_TG = """
INSERT INTO users (telegram_id, is_premium)
SELECT :tg, FALSE
WHERE NOT EXISTS (SELECT 1 FROM users WHERE telegram_id = :tg)
RETURNING id;
"""

_SELECT_USER_ID = "SELECT id FROM users WHERE telegram_id = :tg;"

def init_extras() -> None:
    """Create extra tables if missing (idempotent)."""
    with engine.connect() as conn:
        conn.execute(text(_USER_SETTINGS_DDL))
        conn.commit()

# --- Internal helpers --------------------------------------------------------

def _ensure_user_id(telegram_id: str) -> int:
    """Return user.id for a given telegram_id; create user if not exists."""
    tg = str(telegram_id)
    with session_scope() as s:
        row = s.execute(text(_SELECT_USER_ID), {"tg": tg}).first()
        if row and row.id:
            return int(row.id)
        # create new user record
        row = s.execute(text(_CREATE_USER_BY_TG), {"tg": tg}).first()
        if row and row.id:
            return int(row.id)
        # retry select to be safe (if insert didn't run because user already exists)
        row = s.execute(text(_SELECT_USER_ID), {"tg": tg}).first()
        if not row:
            raise RuntimeError(f"Could not resolve or create user for telegram_id={tg}")
        return int(row.id)

# --- Public API --------------------------------------------------------------

def get_user_setting(telegram_id: str, key: str) -> Optional[str]:
    """
    Read a user-level key/value setting for a Telegram user.
    Returns the value (string) or None if not set.
    """
    uid = _ensure_user_id(telegram_id)
    with session_scope() as s:
        row = s.execute(
            text("SELECT value FROM user_settings WHERE user_id=:uid AND key=:k"),
            {"uid": uid, "k": key},
        ).first()
        return None if not row else row.value

def set_user_setting(telegram_id: str, key: str, value: str) -> None:
    """
    Upsert a user-level key/value setting for a Telegram user.
    """
    uid = _ensure_user_id(telegram_id)
    with session_scope() as s:
        s.execute(
            text("""
            INSERT INTO user_settings (user_id, key, value)
            VALUES (:uid, :k, :v)
            ON CONFLICT (user_id, key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """),
            {"uid": uid, "k": key, "v": value},
        )
