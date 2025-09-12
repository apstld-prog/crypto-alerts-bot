# worker_logic.py
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import Alert, User

log = logging.getLogger("worker_logic")

BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ───────── Symbol resolve ─────────
DEFAULT_QUOTE = "USDT"
KNOWN_QUOTES = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD"}

def resolve_symbol(sym: Optional[str]) -> Optional[str]:
    if not sym:
        return None
    s = sym.strip().upper()
    if "/" in s:
        base, _, quote = s.partition("/")
        base = base.strip()
        quote = (quote.strip() or DEFAULT_QUOTE)
        return f"{base}{quote}"
    for q in KNOWN_QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s
    return f"{s}{DEFAULT_QUOTE}"

def fetch_price(symbol: str) -> float:
    r = requests.get(BINANCE_URL, params={"symbol": symbol.upper()}, timeout=8)
    r.raise_for_status()
    data = r.json()
    return float(data["price"])

def fetch_price_binance(symbol_pair: str) -> float:
    return fetch_price(symbol_pair)

def should_fire(alert: Alert, price: float) -> bool:
    if alert.rule == "price_above":
        return price > alert.value
    if alert.rule == "price_below":
        return price < alert.value
    return False

def send_telegram(chat_id: int, text: str) -> Dict[str, Any]:
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN not set; skipping telegram send")
        return {"ok": False, "reason": "no_token"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=10)
    try:
        j = r.json()
    except Exception:
        j = {"ok": False, "status_code": r.status_code, "body": r.text[:500]}
    if not j.get("ok"):
        log.warning("telegram_send_fail chat=%s resp=%s", chat_id, j)
    return j

def run_alert_cycle(session: Session) -> Dict[str, int]:
    rows = session.execute(
        select(Alert, User).join(User, Alert.user_id == User.id).where(Alert.enabled == True)  # noqa: E712
    ).all()

    evaluated = 0
    triggered = 0
    errors = 0

    symbols = {r.Alert.symbol for r in rows}
    price_cache: Dict[str, Optional[float]] = {}
    for sym in symbols:
        try:
            price_cache[sym] = fetch_price(sym)
        except Exception as e:
            log.warning("price_fetch_fail symbol=%s err=%s", sym, e)
            price_cache[sym] = None

    now = datetime.utcnow()

    for row in rows:
        alert: Alert = row.Alert
        user: User = row.User
        evaluated += 1

        price = price_cache.get(alert.symbol)
        if price is None:
            continue

        met_now = should_fire(alert, price)

        # Cooldown
        if alert.last_fired_at:
            next_ok = alert.last_fired_at + timedelta(seconds=alert.cooldown_seconds)
            in_cooldown = now < next_ok
        else:
            in_cooldown = False

        # Fire on crossing only, and outside cooldown
        should_send = (met_now and (not alert.last_met) and (not in_cooldown))

        if should_send:
            try:
                msg = (
                    f"🔔 <b>Alert #{alert.id}</b>\n"
                    f"Symbol: <b>{alert.symbol}</b>\n"
                    f"Rule: <code>{'>' if alert.rule=='price_above' else '<'} {alert.value}</code>\n"
                    f"Price: <b>{price}</b>\n"
                    f"Time: <code>{now.isoformat(timespec='seconds')}Z</code>"
                )
                send_telegram(int(user.telegram_id), msg)
                alert.last_fired_at = now
                triggered += 1
            except Exception as e:
                log.exception("alert_send_error id=%s err=%s", alert.id, e)
                errors += 1

        alert.last_met = bool(met_now)

    session.flush()
    return {"evaluated": evaluated, "triggered": triggered, "errors": errors}
