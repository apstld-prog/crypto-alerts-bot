# feedback_followup.py
import os
import time
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from db import session_scope

# minutes back to look for triggers
MINUTES_BACK = int(os.getenv("FEEDBACK_LOOKBACK_MINUTES", "1440"))
INTERVAL_SECONDS = int(os.getenv("FEEDBACK_POLL_SECONDS", "60"))

def _has_last_triggered_column(conn) -> bool:
    row = conn.execute(text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='alerts' AND column_name='last_triggered_at'
        LIMIT 1
    """)).first()
    return bool(row)

def _iter_recent_triggers(conn):
    """Yield (alert_id, user_id, symbol, rule, value, price, triggered_at, telegram_id)."""
    lookback = MINUTES_BACK
    if _has_last_triggered_column(conn):
        sql = text(f"""
            SELECT a.id as alert_id, a.user_id, a.symbol, a.rule, a.value,
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
        # Fallback to alert_triggers table
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

def _send_feedback(alert_id: int, user_tg_id: str, symbol: str, rule: str, value: float, triggered_at: datetime):
    # placeholder: μόνο log για τώρα (στείλε με bot αν θέλεις)
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
                    # εδώ θα έκανες fetch ζωντανής τιμής & θα υπολόγιζες "τι θα κέρδιζες"
                    _send_feedback(aid, str(tg), sym, rule, float(val), trig_at)
        except Exception as e:
            print({"msg": "feedback_scheduler_error", "error": str(e)})
        time.sleep(INTERVAL_SECONDS)

def start_feedback_scheduler():
    if os.getenv("RUN_FEEDBACK", "1") != "1":
        print({"msg": "feedback_scheduler_disabled"})
        return
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
