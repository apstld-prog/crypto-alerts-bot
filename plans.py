# plans.py
# Plan logic without leaking ORM objects outside a session.
# Returns plain dataclass values to avoid DetachedInstanceError.

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

from sqlalchemy import text

from db import session_scope

FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT", "10"))


@dataclass(frozen=True)
class PlanInfo:
    user_id: int
    telegram_id: str
    is_admin: bool
    is_premium: bool
    free_limit: int

    @property
    def has_unlimited(self) -> bool:
        return self.is_admin or self.is_premium


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers

def _ensure_user(telegram_id: str) -> int:
    """
    Resolve users.id by telegram_id, creating a user row if not exists.
    Returns integer user_id.
    """
    tg = str(telegram_id)

    with session_scope() as s:
        row = s.execute(text("SELECT id FROM users WHERE telegram_id = :tg"), {"tg": tg}).first()
        if row and row.id:
            return int(row.id)

        # create user (default not premium)
        row = s.execute(
            text(
                """
                INSERT INTO users (telegram_id, is_premium)
                VALUES (:tg, FALSE)
                ON CONFLICT (telegram_id) DO NOTHING
                RETURNING id
                """
            ),
            {"tg": tg},
        ).first()

        if row and row.id:
            return int(row.id)

        # If insert did nothing because it already exists (race), select again
        row2 = s.execute(text("SELECT id FROM users WHERE telegram_id = :tg"), {"tg": tg}).first()
        if not row2:
            raise RuntimeError(f"Could not resolve/create user for telegram_id={tg}")
        return int(row2.id)


def _get_is_premium(user_id: int) -> bool:
    with session_scope() as s:
        row = s.execute(text("SELECT is_premium FROM users WHERE id = :uid"), {"uid": user_id}).first()
        return bool(row.is_premium) if row else False


# ──────────────────────────────────────────────────────────────────────────────
# Public API used by server_combined.py / commands

def build_plan_info(telegram_id: str, admin_ids: set[str]) -> PlanInfo:
    """
    Returns a PlanInfo with only scalar fields (no ORM instances).
    """
    uid = _ensure_user(telegram_id)
    is_admin = (telegram_id in (admin_ids or set()))
    is_premium = _get_is_premium(uid)
    return PlanInfo(
        user_id=uid,
        telegram_id=telegram_id,
        is_admin=is_admin,
        is_premium=is_premium,
        free_limit=FREE_ALERT_LIMIT,
    )


def can_create_alert(plan: PlanInfo) -> Tuple[bool, str, Optional[int]]:
    """
    Returns (allowed, denial_message, remaining_free_slots)
      - If plan.has_unlimited: unlimited alerts allowed
      - Else: limited by FREE_ALERT_LIMIT
    """
    if plan.has_unlimited:
        return True, "", None

    with session_scope() as s:
        row = s.execute(
            text("SELECT COUNT(*) AS c FROM alerts WHERE user_id = :uid AND enabled = TRUE"),
            {"uid": plan.user_id},
        ).first()
        current = int(row.c) if row else 0

    remaining = max(0, plan.free_limit - current)
    if remaining <= 0:
        # Denial message kept simple; the server attaches upgrade button.
        return (
            False,
            f"You have reached your free limit ({plan.free_limit} alerts). Upgrade to Premium for unlimited alerts.",
            0,
        )
    return True, "", remaining


def plan_status_line(plan: PlanInfo) -> str:
    if plan.has_unlimited:
        role = "Admin" if plan.is_admin else "Premium"
        return f"{role} plan — unlimited alerts."
    return f"Free plan — up to {plan.free_limit} alerts."
