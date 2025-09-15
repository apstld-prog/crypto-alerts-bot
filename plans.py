# plans.py
# Central plan/limits logic for Crypto Alerts Bot
# Free users: up to FREE_ALERT_LIMIT active alerts
# Premium (or Admin): unlimited

import os
from dataclasses import dataclass
from typing import Optional, Tuple, Set
from sqlalchemy import select, text
from db import session_scope, User

FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT", "10"))

@dataclass
class PlanInfo:
    user_id: int
    telegram_id: str
    is_admin: bool
    is_premium: bool
    free_limit: int = FREE_ALERT_LIMIT

    @property
    def has_unlimited(self) -> bool:
        return self.is_admin or self.is_premium

def get_or_create_user(telegram_id: str) -> User:
    with session_scope() as s:
        user = s.execute(select(User).where(User.telegram_id == telegram_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=telegram_id, is_premium=False)
            s.add(user)
            s.flush()
        return user

def build_plan_info(telegram_id: str, admin_ids: Set[str]) -> PlanInfo:
    user = get_or_create_user(telegram_id)
    is_admin = telegram_id in admin_ids
    is_premium = bool(user.is_premium or is_admin)
    # Optionally reflect admin→premium in DB for consistency
    if is_admin and not user.is_premium:
        with session_scope() as s:
            s.execute(text("UPDATE users SET is_premium=TRUE WHERE id=:id"), {"id": user.id})
    return PlanInfo(
        user_id=user.id,
        telegram_id=telegram_id,
        is_admin=is_admin,
        is_premium=is_premium,
        free_limit=FREE_ALERT_LIMIT,
    )

def count_active_alerts(user_id: int) -> int:
    with session_scope() as s:
        return s.execute(
            text("SELECT COUNT(*) FROM alerts WHERE user_id=:uid AND enabled = TRUE"),
            {"uid": user_id}
        ).scalar_one()

def can_create_alert(plan: PlanInfo) -> Tuple[bool, Optional[str], Optional[int]]:
    """
    Returns (allowed, denial_message, remaining_slots).
    remaining_slots is None for unlimited plans.
    """
    if plan.has_unlimited:
        return True, None, None
    active = count_active_alerts(plan.user_id)
    remaining = max(0, plan.free_limit - active)
    if active >= plan.free_limit:
        return False, f"Free plan limit reached ({plan.free_limit}). Upgrade for unlimited.", 0
    return True, None, remaining

def premium_gate_text(upgrade_url: Optional[str], base: str = "This feature is for Premium users.") -> str:
    return f"{base} Upgrade: {upgrade_url}" if upgrade_url else base

def plan_status_line(plan: PlanInfo) -> str:
    if plan.has_unlimited:
        return "Plan: Premium (unlimited alerts)"
    active = count_active_alerts(plan.user_id)
    return f"Plan: Free ({active}/{plan.free_limit} active alerts)"
