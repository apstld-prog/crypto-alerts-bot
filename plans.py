# plans.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Tuple
from sqlalchemy import text
from db import session_scope

@dataclass
class PlanInfo:
    user_id: int
    telegram_id: str
    is_admin: bool
    is_premium: bool
    has_unlimited: bool
    alerts_count: int
    trial_expires_at: str | None

def build_plan_info(telegram_id: str, admin_ids: set[str] | None = None) -> PlanInfo:
    admin_ids = admin_ids or set()
    with session_scope() as session:
        row = session.execute(text("SELECT id, is_premium FROM users WHERE telegram_id = :tg"), {"tg": telegram_id}).mappings().first()
        if not row:
            session.execute(text("INSERT INTO users (telegram_id, is_premium, created_at, updated_at) VALUES (:tg, FALSE, NOW(), NOW())"), {"tg": telegram_id})
            row = session.execute(text("SELECT id, is_premium FROM users WHERE telegram_id = :tg"), {"tg": telegram_id}).mappings().first()
        user_id = row["id"]
        is_premium = bool(row["is_premium"])
        alerts_count = int(session.execute(text("SELECT COUNT(*) FROM alerts WHERE user_id = :uid"), {"uid": user_id}).scalar() or 0)

        trial_row = session.execute(
            text("SELECT provider_sub_id FROM subscriptions WHERE user_id = :uid AND provider = 'trial' ORDER BY created_at DESC LIMIT 1"),
            {"uid": user_id}
        ).mappings().first()
        trial_expires = None
        has_unlimited = is_premium
        if trial_row:
            psid = trial_row.get("provider_sub_id")
            try:
                if psid:
                    dt = datetime.fromisoformat(psid)
                    trial_expires = dt.isoformat()
                    if dt > datetime.utcnow():
                        has_unlimited = True
            except Exception:
                trial_expires = None

        is_admin = telegram_id in admin_ids
        if is_admin:
            has_unlimited = True

        return PlanInfo(user_id=user_id, telegram_id=telegram_id, is_admin=is_admin,
                        is_premium=is_premium, has_unlimited=has_unlimited,
                        alerts_count=alerts_count, trial_expires_at=trial_expires)

def can_create_alert(plan: PlanInfo) -> Tuple[bool, str, int | None]:
    # Unlimited during active trial/premium/admin
    if plan.has_unlimited:
        return True, "", None
    return False, "Access restricted. Your trial expired. Contact admin to extend access.", 0

def plan_status_line(plan: PlanInfo) -> str:
    if plan.has_unlimited:
        if plan.trial_expires_at:
            return f"Plan: Trial/Premium — unlimited access until {plan.trial_expires_at} (UTC)."
        return "Plan: Premium/Admin — unlimited access."
    return "Plan: No access (trial expired). Contact admin."
