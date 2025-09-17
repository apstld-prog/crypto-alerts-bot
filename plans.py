# plans.py
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple
from sqlalchemy import text
from db import session_scope

FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT", "10"))

@dataclass
class PlanInfo:
    user_id: int
    telegram_id: str
    is_admin: bool
    is_premium: bool
    has_unlimited: bool
    free_limit: int
    alerts_count: int

def _ensure_user(tg_id: str) -> int:
    with session_scope() as s:
        row = s.execute(text("SELECT id FROM users WHERE telegram_id = :tg"),
                        {"tg": tg_id}).first()
        if row:
            return int(row.id)
        row = s.execute(text("""
            INSERT INTO users (telegram_id, is_premium)
            VALUES (:tg, FALSE)
            RETURNING id
        """), {"tg": tg_id}).first()
        return int(row.id)

def _is_premium_user(uid: int) -> bool:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        # primary flag on users table
        up = s.execute(text("SELECT is_premium FROM users WHERE id=:uid"),
                       {"uid": uid}).scalar()
        if bool(up):
            return True
        # check subscriptions table if υπάρχει
        try:
            row = s.execute(text("""
                SELECT 1
                FROM subscriptions
                WHERE user_id=:uid AND (
                    status_internal='ACTIVE'
                    OR (status_internal='CANCEL_AT_PERIOD_END'
                        AND (keeps_access_until IS NULL OR keeps_access_until > :now))
                )
                LIMIT 1
            """), {"uid": uid, "now": now}).first()
            return bool(row)
        except Exception:
            # table might not exist yet
            return False

def _alerts_count(uid: int) -> int:
    with session_scope() as s:
        try:
            return int(s.execute(text("SELECT COUNT(*) FROM alerts WHERE user_id=:uid"),
                                 {"uid": uid}).scalar() or 0)
        except Exception:
            return 0

def build_plan_info(tg_id: str, admin_ids: set[str]) -> PlanInfo:
    uid = _ensure_user(tg_id)
    is_admin = tg_id in admin_ids
    is_premium = _is_premium_user(uid)
    alerts_cnt = _alerts_count(uid)
    has_unlimited = is_admin or is_premium
    return PlanInfo(
        user_id=uid,
        telegram_id=tg_id,
        is_admin=is_admin,
        is_premium=is_premium,
        has_unlimited=has_unlimited,
        free_limit=FREE_ALERT_LIMIT,
        alerts_count=alerts_cnt,
    )

def can_create_alert(plan: PlanInfo) -> Tuple[bool, str, int | None]:
    if plan.has_unlimited:
        return True, "", None
    remaining = max(0, plan.free_limit - plan.alerts_count)
    if remaining <= 0:
        return False, (f"Free plan limit reached ({plan.free_limit}). "
                       f"Upgrade for unlimited alerts."), 0
    return True, "", remaining

def plan_status_line(plan: PlanInfo) -> str:
    if plan.has_unlimited:
        return "Plan: Premium — unlimited alerts."
    used = plan.alerts_count
    limit = plan.free_limit
    left = max(0, limit - used)
    return f"Plan: Free — {used}/{limit} used ({left} left)."
