import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import Alert

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # optional default


def fetch_price_binance(symbol: str, timeout: int = 8) -> Optional[float]:
    """Fetch latest price from Binance public API. symbol example: BTCUSDT"""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        return float(data["price"])
    except Exception:
        return None


def should_trigger(rule: str, threshold: float, price: float) -> bool:
    if rule == "price_above":
        return price > threshold
    if rule == "price_below":
        return price < threshold
    return False


def can_fire(last_fired_at: Optional[datetime], cooldown_seconds: int) -> bool:
    if last_fired_at is None:
        return True
    return datetime.utcnow() >= last_fired_at + timedelta(seconds=cooldown_seconds)


def notify_telegram(text: str, chat_id: Optional[str] = None, timeout: int = 10) -> bool:
    if not BOT_TOKEN:
        return False
    chat = chat_id or TELEGRAM_CHAT_ID
    if not chat:
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": chat, "text": text}, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def run_alert_cycle(session: Session) -> Dict[str, int]:
    """Evaluate all enabled alerts once. Returns counters for logs/stats."""
    counters = {"evaluated": 0, "triggered": 0, "errors": 0}
    alerts = session.execute(
        select(Alert).where(Alert.enabled == True)  # noqa: E712
    ).scalars().all()

    for alert in alerts:
        counters["evaluated"] += 1
        price = fetch_price_binance(alert.symbol)
        if price is None:
            counters["errors"] += 1
            continue
        if should_trigger(alert.rule, alert.value, price) and can_fire(alert.last_fired_at, alert.cooldown_seconds):
            alert.last_fired_at = datetime.utcnow()
            session.add(alert)
            session.flush()
            counters["triggered"] += 1
            notify_telegram(f"🔔 Alert #{alert.id} | {alert.symbol} {alert.rule} {alert.value} | price={price:.6f}")
    return counters
