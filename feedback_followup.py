# feedback_followup.py
import os
import time
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from db import session_scope

# How far back to scan for recent triggers (minutes)
MINUTES_BACK = int(os.getenv("FEEDBACK_LOOKBACK_MINUTES", "1440"))
# How often to run the feedback scanner (seconds)
INTERVAL_SECONDS = int(os.getenv("FEEDBACK_POLL_SECONDS", "60"))

def _ensure_alert_triggers_schema(conn) -> None:
    """Create alert_triggers table if it doesn't exist."""
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS alert_triggers (
            id BIGSERIAL PRIMARY KEY,
            alert_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            symbol TEXT NOT NULL,
            rule TEXT NOT NULL,
            value NUMERIC NOT NULL,
            price NUMERIC,
            triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_alert_triggers_triggered_at
        ON alert_triggers (triggered_at DESC);
    """))

def _has_last_triggered_column(conn) -> bool:
    row = conn.execute(text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='alerts' AND column_name='last_triggered_at'
        LIMIT 1
    """)).first()
    return bool(row)

def _iter_recent_triggers(conn):
    """
    Yield tuples: (alert_id, user_id, symbol, rule, value, price, triggered_at, telegram_id)
    Uses alerts.last_triggered_at when available; otherwise falls back to alert_triggers.
    """
    lookback = MINUTES_BACK
    if _has_last_triggered_column(conn):
        sql = text(f"""
            SELECT a.id AS alert_id, a.user_id, a.symbol, a.rule, a.value,
                   NULL::NUMERIC AS price,
                   a.last_triggered_at AS triggered_at,
                   u.telegram_id
            FROM alerts a
            JOIN users u ON u.id = a.user_id
            WHERE a.last_triggered_at IS NOT NULL
              AND a.last_triggered_at >= NOW() - INTERVAL '{lookback} minutes'
            ORDER BY a.last_triggered_at DESC
            LIMIT 500
        """)
        for r in conn.execute(sql):
            yield r.alert_id, r.user_id, r.symbol, r.rule, r.value, r.price, r.triggered_at, r.telegram_id
    else:
        # Fallback to alert_triggers
        _ensure_alert_triggers_schema(conn)
        sql = text(f"""
            SELECT t.alert_id, t.user_id, t.symbol, t.rule, t.value, t.price,
                   t.triggered_at, u.telegram_id
            FROM alert_triggers t
            JOIN users u ON u.id = t.user_id
            WHERE t.triggered_at >= NOW() - INTERVAL '{lookback} minutes'
            ORDER BY t.triggered_at DESC
            LIMIT 500
        """)
        for r in conn.execute(sql):
            yield r.alert_id, r.user_id, r.symbol, r.rule, r.value, r.price, r.triggered_at, r.telegram_id

def _send_feedback_sample(alert_id: int, user_tg_id: str, symbol: str, rule: str, value: float, triggered_at: datetime):
    """
    Placeholder for a follow-up message logic (π.χ. "θα είχες κερδίσει Χ%").
    Εδώ απλώς κάνουμε log για να βλέπεις ότι τρέχει.
    """
    print({
        "msg": "feedback_followup_sample",
        "alert_id": alert_id,
        "tg": user_tg_id,
        "symbol": symbol,
        "rule": rule,
        "value": float(value),
        "triggered_at": triggered_at.isoformat() if triggered_at else None
    })

def _loop():
    print({"msg": "feedback_scheduler_started", "minutes_back": MINUTES_BACK, "interval": INTERVAL_SECONDS})
    while True:
        try:
            with session_scope() as s:
                for (aid, uid, sym, rule, val, price, trig_at, tg) in _iter_recent_triggers(s):
                    # TODO: fetch live price & compute PnL idea — εδώ μόνο log
                    _send_feedback_sample(aid, str(tg), sym, rule, float(val), trig_at)
        except Exception as e:
            print({"msg": "feedback_scheduler_error", "error": str(e)})
        time.sleep(INTERVAL_SECONDS)

def start_feedback_scheduler():
    if os.getenv("RUN_FEEDBACK", "1") != "1":
        print({"msg": "feedback_scheduler_disabled"})
        return
    t = threading.Thread(target=_loop, daemon=True)
    t.start()

# ─────────────────────────────────────────────────────────────────────
# Public API used by worker_logic when an alert actually triggers
# ─────────────────────────────────────────────────────────────────────
def record_alert_trigger(session,
                         alert_id: int,
                         user_id: int,
                         symbol: str,
                         rule: str,
                         value: float,
                         price: float | None) -> None:
    """
    Called by worker_logic when an alert fires.
    Persists to alert_triggers and (if present) updates alerts.last_triggered_at.
    """
    now = datetime.now(tz=timezone.utc)

    # Ensure table exists
    _ensure_alert_triggers_schema(session)

    # Insert trigger row
    session.execute(
        text("""
            INSERT INTO alert_triggers (alert_id, user_id, symbol, rule, value, price, triggered_at)
            VALUES (:aid, :uid, :sym, :rule, :val, :price, :ts)
        """),
        {"aid": alert_id, "uid": user_id, "sym": symbol, "rule": rule, "val": value, "price": price, "ts": now}
    )

    # Try to update alerts.last_triggered_at if the column exists
    if _has_last_triggered_column(session):
        session.execute(
            text("UPDATE alerts SET last_triggered_at=:ts WHERE id=:aid"),
            {"ts": now, "aid": alert_id}
        )
    # (το commit θα το κάνει ο caller ή το session_scope upstream)
