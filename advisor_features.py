# advisor_features.py
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy import text

from db import session_scope

# Try to reuse project price util
try:
    from worker_logic import fetch_price_binance
except Exception:
    fetch_price_binance = None

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DAILY_HOUR_UTC = int(os.getenv("ADVISOR_DAILY_UTC_HOUR", "9"))  # default 09:00 UTC

_BINANCE_HOSTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

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


def _http_get_json(url: str, timeout: float = 10.0) -> Optional[dict]:
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None

def _price_usdt(base: str) -> Optional[float]:
    pair = f"{base.upper()}USDT"
    if fetch_price_binance:
        try:
            p = fetch_price_binance(pair)
            if p is not None:
                return float(p)
        except Exception:
            pass
    for host in _BINANCE_HOSTS:
        data = _http_get_json(f"{host}/api/v3/ticker/price?symbol={pair}")
        try:
            if data and "price" in data:
                return float(data["price"])
        except Exception:
            continue
    return None


def _format_alloc_live(budget: float, risk: str) -> str:
    risk = (risk or "medium").lower()
    if risk == "low":
        weights = {"BTC": 0.70, "ETH": 0.20, "ALTS": 0.10}
    elif risk == "high":
        weights = {"BTC": 0.30, "ETH": 0.30, "ALTS": 0.40}
    else:
        weights = {"BTC": 0.50, "ETH": 0.30, "ALTS": 0.20}

    btc_p = _price_usdt("BTC")
    eth_p = _price_usdt("ETH")

    def _line(asset: str, w: float) -> str:
        usd = budget * w
        if asset in {"BTC", "ETH"}:
            spot = btc_p if asset == "BTC" else eth_p
            if spot and spot > 0:
                units = usd / spot
                return f"• {asset}: <b>{int(w*100)}%</b>  (~{usd:.2f})  ≈ <b>{units:.6f} {asset}</b> @ {spot:.2f}"
        return f"• {asset}: <b>{int(w*100)}%</b>  (~{usd:.2f})"

    lines = [
        "<b>Daily Advisor</b>",
        f"• Budget: <b>{budget:.2f}</b>",
        f"• Risk: <b>{risk}</b>",
        "",
        "<b>Suggested Allocation</b>:",
        _line("BTC", weights["BTC"]),
        _line("ETH", weights["ETH"]),
        _line("ALTS", weights["ALTS"]),
    ]
    return "\n".join(lines)


def _next_sleep_seconds(now: datetime) -> float:
    target = now.replace(hour=DAILY_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


def start_advisor_scheduler() -> None:
    """
    Background loop that sends a daily advisor message with live quantities
    to all users who have an advisor profile.
    """
    def _loop():
        print({"msg": "daily_advisor_scheduler_started", "hour_utc": DAILY_HOUR_UTC})
        time.sleep(5)
        while True:
            try:
                sleep_seconds = _next_sleep_seconds(datetime.utcnow())
                time.sleep(max(5.0, min(sleep_seconds, 24 * 3600)))

                with session_scope() as s:
                    rows = s.execute(text("""
                        SELECT ap.user_id, ap.risk, ap.budget, u.telegram_id
                        FROM advisor_profiles ap
                        JOIN users u ON u.id = ap.user_id
                    """)).all()

                sent = 0
                for r in rows:
                    html = _format_alloc_live(float(r.budget), r.risk)
                    _send_message(str(r.telegram_id), html)
                    sent += 1

                print({"msg": "daily_advisor_sent", "count": sent})
            except Exception as e:
                print({"msg": "daily_advisor_error", "error": str(e)})
                time.sleep(30)

    import threading
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
