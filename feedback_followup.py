# feedback_followup.py
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from db import session_scope

# Send follow-up messages (e.g., +2h / +6h) after an alert triggered
# This version normalizes ALL timestamps to UTC-naive for arithmetic,
# avoiding "can't subtract offset-naive and offset-aware datetimes".

FOLLOWUP_DELAYS = [2 * 3600, 6 * 3600]  # seconds after trigger


def _utcnow_naive() -> datetime:
    # Always produce naive-UTC
    return datetime.utcnow().replace(tzinfo=None)


def _ensure_tables():
    with session_scope() as s:
        # Table to track followups we have already sent
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS alert_followups (
                alert_id BIGINT NOT NULL,
                stage INT NOT NULL,
                sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (alert_id, stage)
            );
        """))
        s.commit()


def _send_followup_message(telegram_id: int, text_msg: str):
    # Keep it simple; server_combined uses main bot loop to send alerts
    # We only store the work item; the actual sending can be a simple HTTP call.
    # To keep dependency-free here, we emit a row in an outbox table used by the main code.
    with session_scope() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS outbox_msgs (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                status TEXT NOT NULL DEFAULT 'new'
            );
        """))
        s.execute(text(
            "INSERT INTO outbox_msgs (chat_id, body) VALUES (:cid, :body)"
        ), {"cid": telegram_id, "body": text_msg})
        s.commit()


def _load_recent_triggers(limit_minutes: int = 24 * 60):
    """Return recent alerts that triggered in the last N minutes with (id, user_id, telegram_id, symbol, rule, value, last_triggered_at)."""
    with session_scope() as s:
        rows = s.execute(text(f"""
            SELECT a.id, a.user_id, u.telegram_id, a.symbol, a.rule, a.value, a.last_triggered_at
            FROM alerts a
            JOIN users u ON u.id = a.user_id
            WHERE a.last_triggered_at IS NOT NULL
              AND a.last_triggered_at >= NOW() - INTERVAL '{limit_minutes} minutes'
            ORDER BY a.last_triggered_at DESC
            LIMIT 500
        """)).all()
    return rows


def _already_sent(alert_id: int, stage: int) -> bool:
    with session_scope() as s:
        row = s.execute(text(
            "SELECT 1 FROM alert_followups WHERE alert_id=:aid AND stage=:st"
        ), {"aid": alert_id, "st": stage}).first()
    return bool(row)


def _mark_sent(alert_id: int, stage: int):
    with session_scope() as s:
        s.execute(text(
            "INSERT INTO alert_followups (alert_id, stage) VALUES (:aid, :st)"
        ), {"aid": alert_id, "st": stage})
        s.commit()


def _format_followup(symbol: str, rule: str, value: float, hours_after: int) -> str:
    op = ">" if rule == "price_above" else "<"
    return (
        f"📈 Follow-up ({hours_after}h)\n"
        f"Symbol: {symbol}\n"
        f"Rule: {op} {value}\n"
        f"(This is an informational performance follow-up.)"
    )


def start_feedback_scheduler():
    """
    Background loop: check for recently-triggered alerts, and send follow-ups
    at +2h and +6h if not already sent.
    """
    def _loop():
        print({"msg": "worker_extra_threads_started"})
        _ensure_tables()
        time.sleep(3)
        while True:
            try:
                now = _utcnow_naive()
                recent = _load_recent_triggers()
                for r in recent:
                    # Normalize DB timestamp to naive UTC for subtraction
                    last_trig = r.last_triggered_at
                    if last_trig is None:
                        continue
                    # Convert any tz-aware to naive
                    if getattr(last_trig, "tzinfo", None) is not None:
                        last_trig = last_trig.replace(tzinfo=None)
                    elapsed = (now - last_trig).total_seconds()
                    for idx, delay in enumerate(FOLLOWUP_DELAYS, start=1):
                        if elapsed >= delay and not _already_sent(r.id, idx):
                            hrs = int(delay // 3600)
                            _send_followup_message(r.telegram_id, _format_followup(r.symbol, r.rule, r.value, hrs))
                            _mark_sent(r.id, idx)
                time.sleep(30)
            except Exception as e:
                print({"msg": "feedback_scheduler_error", "error": str(e)})
                time.sleep(10)

    import threading
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
