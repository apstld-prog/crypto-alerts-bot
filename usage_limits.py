# usage_limits.py
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from db import session_scope


DEFAULT_FREE_LIMIT = 10  # free uses per command


@dataclass
class UsageResult:
    allowed: bool
    used: int
    remaining: int
    limit: int


def ensure_usage_schema() -> None:
    """Create the user_usage table if it doesn't exist."""
    with session_scope() as s:
        s.execute(text(
            """
            CREATE TABLE IF NOT EXISTS user_usage (
                user_id BIGINT NOT NULL,
                command TEXT NOT NULL,
                cnt INT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, command)
            );
            """
        ))
        s.commit()


def increment_and_check(user_id: int, command: str, is_premium: bool, limit: int = DEFAULT_FREE_LIMIT) -> UsageResult:
    """
    Increment usage for (user, command) and return allowance result.
    Premium users are always allowed and we don't store their usage.
    For Free users, we increment first and then compare against limit.
    """
    if is_premium:
        # Unlimited: short-circuit
        return UsageResult(allowed=True, used=0, remaining=-1, limit=-1)

    ensure_usage_schema()
    cmd = (command or "").strip().lower()

    with session_scope() as s:
        # Upsert-like increment
        row = s.execute(text(
            "SELECT cnt FROM user_usage WHERE user_id=:uid AND command=:cmd"
        ), {"uid": user_id, "cmd": cmd}).first()

        if row:
            new_cnt = int(row.cnt) + 1
            s.execute(text(
                "UPDATE user_usage SET cnt=:c, updated_at=NOW() WHERE user_id=:uid AND command=:cmd"
            ), {"c": new_cnt, "uid": user_id, "cmd": cmd})
        else:
            new_cnt = 1
            s.execute(text(
                "INSERT INTO user_usage (user_id, command, cnt) VALUES (:uid, :cmd, 1)"
            ), {"uid": user_id, "cmd": cmd})
        s.commit()

    allowed = new_cnt <= int(limit)
    remaining = max(0, int(limit) - new_cnt)
    return UsageResult(allowed=allowed, used=new_cnt, remaining=remaining, limit=int(limit))
