# worker_logic.py
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Dict, Any

import requests
from sqlalchemy import text

from db import session_scope
from feedback_followup import record_alert_trigger

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
# How often the server's alerts loop calls this (seconds) comes from the caller,
# but we also guard with per-alert cooldowns in DB.
DEFAULT_COOLDOWN = int(os.getenv("ALERT_DEFAULT_COOLDOWN_SECONDS", "900"))  # 15m fallback


# ────────────────────────────────────────────────────────────────────
# Price helpers

def resolve_symbol(symbol: str | None) -> str | None:
    """
    Return a Binance USDT pair symbol if we know it, else None.
    Caller may additionally try auto-discovery (exchangeInfo) if needed.
    """
    if not symbol:
        return None
    s = symbol.strip().upper()
    if s.endswith("USDT"):
        return s
    # minimal heuristic: assume USDT if not specified
    return f"{s}USDT"


def fetch_price_binance(symbol_pair: str) -> float | None:
    """
    Lightweight spot price from Binance public API.
    """
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol_pair},
            timeout=10,
        )
        if r.status_code == 200:
            j = r.json()
            return float(j.get("price"))
    except Exception:
        return None
    return None


# ────────────────────────────────────────────────────────────────────
# Telegram send helper

def _send_telegram_message(chat_id: str, html: str, reply_markup: Dict[str, Any] | None = None) -> bool:
    if not BOT_TOKEN:
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(url, json=payload, timeout=15)
        ok = r.status_code == 200 and r.json().get("ok") is True
        if not ok:
            print({"msg": "send_alert_message_fail", "chat_id": chat_id, "status": r.status_code, "body": r.text[:200]})
        return ok
    except Exception as e:
        print({"msg": "send_alert_message_exception", "chat_id": chat_id, "error": str(e)})
        return False


def _ack_inline_buttons(alert_id: int):
    # (same callback scheme used by server_combined.on_callback)
    return {
        "inline_keyboard": [
            [
                {"text": "👍 Keep", "callback_data": f"ack:keep:{alert_id}"},
                {"text": "🗑️ Delete", "callback_data": f"ack:del:{alert_id}"},
            ]
        ]
    }


# ────────────────────────────────────────────────────────────────────
# Main alert runner

def run_alert_cycle(session) -> Dict[str, int]:
    """
    Evaluate user alerts and send notifications.
    Must be called with an active SQLAlchemy session bound to the same engine as db.session_scope.
    Returns counters for logging.
    """
    evaluated = 0
    triggered = 0
    errors = 0

    # Schema assumptions:
    #  - users(id BIGSERIAL PK, telegram_id TEXT UNIQUE)
    #  - alerts(
    #       id BIGSERIAL PK,
    #       user_id BIGINT,
    #       symbol TEXT,              -- e.g. BTCUSDT
    #       rule TEXT,                -- 'price_above' | 'price_below'
    #       value NUMERIC,            -- threshold
    #       cooldown_seconds INT,     -- NULL/0 -> DEFAULT_COOLDOWN
    #       enabled BOOLEAN,
    #       last_fired_at TIMESTAMPTZ NULL,
    #       user_seq INT              -- optional per-user numbering
    #    )
    #
    # Any differences with your existing schema are easy to patch; the queries below are conservative.

    rows = session.execute(text(
        """
        SELECT a.id, a.user_id, a.symbol, a.rule, a.value, a.cooldown_seconds,
               a.enabled, a.last_fired_at,
               COALESCE(u.telegram_id, '') AS telegram_id
        FROM alerts a
        JOIN users u ON u.id = a.user_id
        WHERE a.enabled = TRUE
        ORDER BY a.id ASC
        LIMIT 500
        """
    )).all()

    for r in rows:
        evaluated += 1
        try:
            symbol = r.symbol
            pair = symbol if symbol and symbol.endswith("USDT") else resolve_symbol(symbol)
            if not pair:
                continue

            price = fetch_price_binance(pair)
            if price is None:
                continue

            rule = r.rule or ""
            threshold = float(r.value)

            # cooldown
            cooldown = int(r.cooldown_seconds or 0) or DEFAULT_COOLDOWN
            last_ts = r.last_fired_at
            if last_ts is not None:
                diff = datetime.utcnow() - last_ts
                if diff.total_seconds() < cooldown:
                    # still cooling
                    continue

            # evaluate
            ok = False
            if rule == "price_above":
                ok = price > threshold
            elif rule == "price_below":
                ok = price < threshold

            if not ok:
                continue

            # Triggered → send notification + update last_fired_at + store snapshot for feedback
            html = (
                f"🔔 <b>{pair}</b> {('>' if rule=='price_above' else '<')} {threshold}\n"
                f"Now: <b>{price:.6f}</b>"
            )
            sent = False
            if r.telegram_id:
                sent = _send_telegram_message(str(r.telegram_id), html, reply_markup=_ack_inline_buttons(r.id))

            # Update cooldown regardless of send success to avoid spamming when chat_id is invalid
            session.execute(text(
                "UPDATE alerts SET last_fired_at = NOW() WHERE id = :id"
            ), {"id": r.id})
            session.commit()

            # Record for feedback loop
            try:
                record_alert_trigger(
                    alert_id=int(r.id),
                    user_id=int(r.user_id),
                    symbol=pair,
                    rule=rule,
                    threshold=threshold,
                    trigger_price=float(price),
                )
            except Exception as e:
                print({"msg": "record_alert_trigger_error", "id": r.id, "error": str(e)})

            if sent:
                triggered += 1

        except Exception as e:
            errors += 1
            print({"msg": "alert_eval_error", "id": getattr(r, "id", None), "error": str(e)})

    return {"evaluated": evaluated, "triggered": triggered, "errors": errors}
