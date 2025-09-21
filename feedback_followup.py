# feedback_followup.py
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import requests
from sqlalchemy import text

from db import session_scope

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
# Windows (hours) after trigger to send follow-up PnL
FEEDBACK_WINDOWS = os.getenv("FEEDBACK_WINDOWS", "2,6")


def _ensure_tables():
    with session_scope() as s:
        s.execute(text(
            """
            CREATE TABLE IF NOT EXISTS alert_triggers (
                id BIGSERIAL PRIMARY KEY,
                alert_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                symbol TEXT NOT NULL,
                rule TEXT NOT NULL,
                threshold NUMERIC NOT NULL,
                trigger_price NUMERIC NOT NULL,
                triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sent_2h BOOLEAN NOT NULL DEFAULT FALSE,
                sent_6h BOOLEAN NOT NULL DEFAULT FALSE
            );
            """
        ))
        s.execute(text(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                telegram_id TEXT UNIQUE
            );
            """
        ))
        s.commit()


def record_alert_trigger(alert_id: int, user_id: int, symbol: str, rule: str, threshold: float, trigger_price: float):
    """Called when an alert fires, to store a snapshot for follow-up."""
    _ensure_tables()
    with session_scope() as s:
        s.execute(text(
            """
            INSERT INTO alert_triggers (alert_id, user_id, symbol, rule, threshold, trigger_price, triggered_at)
            VALUES (:aid, :uid, :sym, :rule, :thr, :p, NOW())
            """
        ), {"aid": alert_id, "uid": user_id, "sym": symbol, "rule": rule, "thr": threshold, "p": trigger_price})
        s.commit()


def _send_dm(telegram_id: str, html: str):
    if not BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": telegram_id,
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
    except Exception:
        pass


def _fetch_price(symbol_pair: str) -> float | None:
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": symbol_pair}, timeout=10)
        if r.status_code == 200:
            j = r.json()
            return float(j.get("price"))
    except Exception:
        return None
    return None


def _fmt(p: float) -> str:
    if p >= 1:
        return f"{p:.2f}"
    return f"{p:.6f}"


def feedback_scheduler_loop():
    _ensure_tables()
    windows = []
    for w in FEEDBACK_WINDOWS.split(","):
        w = w.strip()
        if not w:
            continue
        try:
            windows.append(int(w))
        except Exception:
            pass
    windows = sorted(set([w for w in windows if 1 <= w <= 48]))  # clamp 1..48h
    print({"msg": "feedback_scheduler_started", "windows": windows})

    while True:
        try:
            now = datetime.utcnow()
            with session_scope() as s:
                # bring recent triggers from last 48h
                rows = s.execute(text(
                    """
                    SELECT t.id, t.alert_id, t.user_id, t.symbol, t.rule, t.threshold, t.trigger_price, t.triggered_at,
                           u.telegram_id, t.sent_2h, t.sent_6h
                    FROM alert_triggers t
                    JOIN users u ON t.user_id = u.id
                    WHERE t.triggered_at > NOW() - INTERVAL '48 hours'
                    ORDER BY t.triggered_at ASC
                    """
                )).all()
            for r in rows:
                pair = r.symbol  # already like BTCUSDT
                now_price = _fetch_price(pair)
                if now_price is None:
                    continue
                delta_pct = (now_price - float(r.trigger_price)) / float(r.trigger_price) * 100.0
                # We don't know user's side (long/short). Show both interpretations.
                gr = (
                    f"🔁 Alert feedback • {pair}\n"
                    f"Trigger @ {_fmt(float(r.trigger_price))}  →  Now {_fmt(now_price)}  ({delta_pct:+.2f}%)\n"
                    f"• Αν είχες long: {delta_pct:+.2f}%   • Αν είχες short: {-delta_pct:+.2f}%"
                )
                en = (
                    f"🔁 Alert feedback • {pair}\n"
                    f"Trigger @ {_fmt(float(r.trigger_price))}  →  Now {_fmt(now_price)}  ({delta_pct:+.2f}%)\n"
                    f"• If long: {delta_pct:+.2f}%   • If short: {-delta_pct:+.2f}%"
                )

                # Check each window and send once
                age = now - r.triggered_at
                updates = {}
                should_send = None
                if (2 in windows) and (age >= timedelta(hours=2)) and (not r.sent_2h):
                    updates["sent_2h"] = True; should_send = "2h"
                if (6 in windows) and (age >= timedelta(hours=6)) and (not r.sent_6h):
                    updates["sent_6h"] = True; should_send = "6h"  # prefer latest if both true

                if updates and should_send:
                    tag = "⏱️ +2h" if should_send == "2h" else "⏱️ +6h"
                    _send_dm(r.telegram_id, f"{tag}\n{gr}\n— — —\n{en}")
                    # persist sent flag(s)
                    with session_scope() as s2:
                        sets = []
                        params = {"id": r.id}
                        for k, v in updates.items():
                            sets.append(f"{k} = :{k}")
                            params[k] = v
                        s2.execute(text(f"UPDATE alert_triggers SET {', '.join(sets)} WHERE id=:id"), params)
                        s2.commit()
        except Exception as e:
            print({"msg": "feedback_scheduler_error", "error": str(e)})
        time.sleep(30)


def start_feedback_scheduler():
    import threading
    t = threading.Thread(target=feedback_scheduler_loop, daemon=True)
    t.start()
