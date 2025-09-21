# feedback_followup.py
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text

from db import session_scope

# Follow-up σήματα που στέλνουμε μετά από alert trigger
# π.χ. +2 ώρες και +6 ώρες
FOLLOWUP_DELAYS = [2 * 3600, 6 * 3600]  # seconds


# ───────────────────────── Time helpers (UTC-naive) ─────────────────────────

def _utcnow_naive() -> datetime:
    """Return naive UTC datetime (no tzinfo) to avoid aware/naive mix errors."""
    return datetime.utcnow().replace(tzinfo=None)


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is not None:
        return dt.replace(tzinfo=None)
    return dt


# ─────────────────────── Schema helpers (create if missing) ─────────────────

def _ensure_tables() -> None:
    """Create tables used by the feedback system if they don't exist."""
    with session_scope() as s:
        # Πίνακας με στιγμές που πυροδοτήθηκαν alerts
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS alert_triggers (
                id BIGSERIAL PRIMARY KEY,
                alert_id BIGINT NOT NULL,
                user_id BIGINT,
                symbol TEXT,
                rule TEXT,
                value NUMERIC,
                price NUMERIC,
                triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """))

        # Πίνακας με follow-ups που έχουν σταλεί (για να μη στέλνονται διπλά)
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS alert_followups (
                alert_id BIGINT NOT NULL,
                stage INT NOT NULL,
                sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (alert_id, stage)
            );
        """))

        # Outbox για εύκολη αποστολή μέσω του κύριου bot loop
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS outbox_msgs (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                status TEXT NOT NULL DEFAULT 'new'
            );
        """))
        s.commit()


# ─────────────────────── Public API (used by worker_logic) ───────────────────

def record_alert_trigger(*args: Any, **kwargs: Any) -> None:
    """
    Safe logger used by worker_logic when an alert fires.
    Accepts flexible signature to remain compatible:

        record_alert_trigger(alert_id, user_id, symbol, rule, value, price, ts_utc=None)
    or
        record_alert_trigger(alert_id=<id>, user_id=<uid>, symbol=..., rule=..., value=..., price=..., ts_utc=<dt>)

    Stores a row in alert_triggers and updates alerts.last_triggered_at.
    """
    _ensure_tables()

    # Extract fields defensively
    alert_id = kwargs.get("alert_id", args[0] if len(args) > 0 else None)
    user_id = kwargs.get("user_id", args[1] if len(args) > 1 else None)
    symbol = kwargs.get("symbol", args[2] if len(args) > 2 else None)
    rule   = kwargs.get("rule",   args[3] if len(args) > 3 else None)
    value  = kwargs.get("value",  args[4] if len(args) > 4 else None)
    price  = kwargs.get("price",  args[5] if len(args) > 5 else None)
    ts     = kwargs.get("ts_utc", None)

    ts_naive = _to_naive_utc(ts) or _utcnow_naive()

    if alert_id is None:
        # Χωρίς id δεν μπορούμε να ενημερώσουμε την ειδοποίηση
        return

    try:
        with session_scope() as s:
            # Insert trigger row
            s.execute(text("""
                INSERT INTO alert_triggers (alert_id, user_id, symbol, rule, value, price, triggered_at)
                VALUES (:aid, :uid, :sym, :rule, :val, :price, :ts)
            """), {
                "aid": alert_id,
                "uid": user_id,
                "sym": symbol,
                "rule": rule,
                "val": value,
                "price": price,
                "ts": ts_naive,  # SQLAlchemy θα το περάσει ως timestamptz
            })

            # Update last_triggered_at στο alerts
            s.execute(text("""
                UPDATE alerts
                SET last_triggered_at = :ts
                WHERE id = :aid
            """), {"ts": ts_naive, "aid": alert_id})

            s.commit()
    except Exception as e:
        # Δεν ρίχνουμε exception προς τα πάνω για να μην «σπάσουν» τα alerts
        print({"msg": "record_alert_trigger_error", "error": str(e), "aid": alert_id})


# ─────────────────────── Follow-up sender helpers ────────────────────────────

def _already_sent(alert_id: int, stage: int) -> bool:
    with session_scope() as s:
        row = s.execute(text(
            "SELECT 1 FROM alert_followups WHERE alert_id=:aid AND stage=:st"
        ), {"aid": alert_id, "st": stage}).first()
    return bool(row)


def _mark_sent(alert_id: int, stage: int) -> None:
    with session_scope() as s:
        s.execute(text(
            "INSERT INTO alert_followups (alert_id, stage) VALUES (:aid, :st)"
        ), {"aid": alert_id, "st": stage})
        s.commit()


def _queue_followup_message(telegram_id: int, text_msg: str) -> None:
    # Βάζουμε μήνυμα στην outbox, το οποίο στέλνεται από το main service
    with session_scope() as s:
        s.execute(text(
            "INSERT INTO outbox_msgs (chat_id, body) VALUES (:cid, :body)"
        ), {"cid": telegram_id, "body": text_msg})
        s.commit()


def _format_followup(symbol: str, rule: str, value: float, hours_after: int) -> str:
    op = ">" if (rule or "").lower() == "price_above" else "<"
    return (
        f"📈 Follow-up ({hours_after}h)\n"
        f"Symbol: {symbol}\n"
        f"Rule: {op} {value}\n"
        f"(Performance check after trigger)"
    )


def _load_recent_triggers(window_minutes: int = 24 * 60):
    """
    Βρίσκει πρόσφατα triggers (τελευταίο 24ωρο default) μαζί με user chat.
    Προτιμούμε τον πίνακα alert_triggers για ακρίβεια, αλλά κρατάμε και fallback.
    """
    with session_scope() as s:
        rows = s.execute(text(f"""
            SELECT t.alert_id, t.user_id, t.symbol, t.rule, t.value, t.price, t.triggered_at, u.telegram_id
            FROM alert_triggers t
            JOIN users u ON u.id = t.user_id
            WHERE t.triggered_at >= NOW() - INTERVAL '{window_minutes} minutes'
            ORDER BY t.triggered_at DESC
            LIMIT 500
        """)).all()

        if rows:
            return rows

        # Fallback: αν δεν υπάρχουν rows στον trigger πίνακα, κοιτάμε το alerts.last_triggered_at
        rows = s.execute(text(f"""
            SELECT a.id as alert_id, a.user_id, a.symbol, a.rule, a.value, NULL::NUMERIC as price,
                   a.last_triggered_at as triggered_at, u.telegram_id
            FROM alerts a
            JOIN users u ON u.id = a.user_id
            WHERE a.last_triggered_at IS NOT NULL
              AND a.last_triggered_at >= NOW() - INTERVAL '{window_minutes} minutes'
            ORDER BY a.last_triggered_at DESC
            LIMIT 500
        """)).all()
        return rows


# ───────────────────────── Scheduler main loop ───────────────────────────────

def start_feedback_scheduler() -> None:
    """
    Background loop: κοιτάει πρόσφατα triggers και στέλνει follow-ups
    στα +2h και +6h (αν δεν έχουν ήδη σταλεί).
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
                    trig = _to_naive_utc(r.triggered_at)
                    if trig is None:
                        continue
                    elapsed = (now - trig).total_seconds()
                    for idx, delay in enumerate(FOLLOWUP_DELAYS, start=1):
                        if elapsed >= delay and not _already_sent(r.alert_id, idx):
                            hrs = int(delay // 3600)
                            _queue_followup_message(
                                r.telegram_id,
                                _format_followup(r.symbol, r.rule, r.value, hrs)
                            )
                            _mark_sent(r.alert_id, idx)
                time.sleep(30)
            except Exception as e:
                print({"msg": "feedback_scheduler_error", "error": str(e)})
                time.sleep(10)

    import threading
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
