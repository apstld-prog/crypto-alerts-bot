# advisor_features.py
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import requests
from sqlalchemy import text

from db import session_scope

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DAILY_HOUR_UTC = int(os.getenv("ADVISOR_DAILY_UTC_HOUR", "9"))  # send once per day at this UTC hour


def _send_message(telegram_id: str, html: str) -> None:
    if not BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": telegram_id, "text": html, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as e:
        print({"msg": "advisor_send_error", "error": str(e), "chat_id": telegram_id})


def _format_alloc(budget: float, risk: str) -> str:
    risk = (risk or "medium").lower()
    if risk == "low":
        weights = {"BTC": 0.70, "ETH": 0.20, "ALTS": 0.10}
    elif risk == "high":
        weights = {"BTC": 0.30, "ETH": 0.30, "ALTS": 0.40}
    else:
        weights = {"BTC": 0.50, "ETH": 0.30, "ALTS": 0.20}
    lines = ["<b>Daily Advisor</b>", f"• Budget: <b>{budget:.2f}</b>", f"• Risk: <b>{risk}</b>", "", "<b>Suggested Allocation</b>:"]
    for k, w in weights.items():
        lines.append(f"• {k}: <b>{int(w*100)}%</b>  (~{budget*w:.2f})")
    return "\n".join(lines)


def _next_run_after(now: datetime) -> float:
    """Return seconds to sleep until next DAILY_HOUR_UTC occurrence."""
    target = now.replace(hour=DAILY_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


def start_advisor_scheduler() -> None:
    """
    Start a background loop that sends a daily advisor message to all users
    who have an advisor profile stored.
    """
    def _loop():
        print({"msg": "daily_advisor_scheduler_started", "hour_utc": DAILY_HOUR_UTC})
        # small initial sleep to avoid flooding right on boot
        time.sleep(5)
        while True:
            try:
                sleep_seconds = _next_run_after(datetime.utcnow())
                time.sleep(max(5.0, min(sleep_seconds, 24 * 3600)))
                # Fetch (user_id, risk, budget, telegram_id)
                with session_scope() as s:
                    rows = s.execute(text("""
                        SELECT ap.user_id, ap.risk, ap.budget, u.telegram_id
                        FROM advisor_profiles ap
                        JOIN users u ON u.id = ap.user_id
                    """)).all()
                for r in rows:
                    html = _format_alloc(float(r.budget), r.risk)
                    _send_message(str(r.telegram_id), html)
                print({"msg": "daily_advisor_sent", "count": len(rows)})
            except Exception as e:
                print({"msg": "daily_advisor_error", "error": str(e)})
                time.sleep(30)

    import threading
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
