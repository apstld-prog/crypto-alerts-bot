# worker_logic.py
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import Alert, User, Subscription

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # optional fallback

SYMBOL_MAP = {
    "BTC":"BTCUSDT","ETH":"ETHUSDT","BNB":"BNBUSDT","XRP":"XRPUSDT","ADA":"ADAUSDT","SOL":"SOLUSDT","DOGE":"DOGEUSDT",
    "TRX":"TRXUSDT","DOT":"DOTUSDT","MATIC":"MATICUSDT","LTC":"LTCUSDT","BCH":"BCHUSDT","LINK":"LINKUSDT","XLM":"XLMUSDT",
    "ATOM":"ATOMUSDT","AVAX":"AVAXUSDT","ETC":"ETCUSDT","XMR":"XMRUSDT","XTZ":"XTZUSDT","AAVE":"AAVEUSDT",
    "ALGO":"ALGOUSDT","NEAR":"NEARUSDT","FIL":"FILUSDT","VET":"VETUSDT","ICP":"ICPUSDT","SAND":"SANDUSDT",
    "MANA":"MANAUSDT","AXS":"AXSUSDT","EGLD":"EGLDUSDT","THETA":"THETAUSDT","HBAR":"HBARUSDT","KLAY":"KLAYUSDT",
    "FLOW":"FLOWUSDT","CHZ":"CHZUSDT","EOS":"EOSUSDT","ENJ":"ENJUSDT","ZEC":"ZECUSDT","DASH":"DASHUSDT",
    "COMP":"COMPUSDT","SNX":"SNXUSDT","CRV":"CRVUSDT","SUSHI":"SUSHIUSDT","UNI":"UNIUSDT","MKR":"MKRUSDT",
    "RUNE":"RUNEUSDT","CAKE":"CAKEUSDT","FTM":"FTMUSDT","GRT":"GRTUSDT","ONE":"ONEUSDT","QTUM":"QTUMUSDT",
    "OP":"OPUSDT","ARB":"ARBUSDT","SHIB":"SHIBUSDT","PEPE":"PEPEUSDT","BONK":"BONKUSDT","TIA":"TIAUSDT",
    "SEI":"SEIUSDT","WIF":"WIFUSDT","JUP":"JUPUSDT","PYTH":"PYTHUSDT","SUI":"SUIUSDT","APT":"APTUSDT","INJ":"INJUSDT",
    "RNDR":"RNDRUSDT","ROSE":"ROSEUSDT","AKT":"AKTUSDT","KAS":"KASUSDT","JASMY":"JASMYUSDT","IMX":"IMXUSDT"
}

def resolve_symbol(sym: str) -> Optional[str]:
    s = (sym or "").upper().replace("/", "").strip()
    if s.endswith("USDT"):
        return s
    return SYMBOL_MAP.get(s)

def fetch_price_binance(symbol: str, timeout: int = 8) -> Optional[float]:
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": symbol}, timeout=timeout)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None

def should_trigger(rule: str, threshold: float, price: float) -> bool:
    return (price > threshold) if rule == "price_above" else (price < threshold)

def can_fire(last_fired_at: Optional[datetime], cooldown_seconds: int) -> bool:
    return (last_fired_at is None) or (datetime.utcnow() >= last_fired_at + timedelta(seconds=cooldown_seconds))

def _telegram_send(text: str, chat_id: str, timeout: int = 10) -> Tuple[bool, int, str]:
    if not BOT_TOKEN or not chat_id:
        return (False, 0, "missing token or chat_id")
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": text}, timeout=timeout)
        return (r.status_code == 200, r.status_code, r.text[:500])
    except Exception as e:
        return (False, 0, f"exception: {e}")

def downgrade_expired_premiums(session: Session) -> int:
    # (ίδιο όπως πριν)
    now = datetime.utcnow()
    changed = 0
    users: List[User] = session.execute(select(User)).scalars().all()
    for u in users:
        sub = session.execute(
            select(Subscription).where(Subscription.user_id == u.id).order_by(Subscription.id.desc())
        ).scalar_one_or_none()
        if not sub:
            continue
        if sub.status_internal in ("ACTIVE", "CANCEL_AT_PERIOD_END") and sub.current_period_end and sub.current_period_end < now:
            if u.is_premium:
                u.is_premium = False
                session.add(u)
            session.add(Subscription(
                user_id=u.id, provider="paypal", provider_status="EXPIRED",
                status_internal="CANCELLED", provider_ref=sub.provider_ref,
                current_period_end=sub.current_period_end,
            ))
            changed += 1
    return changed

def run_alert_cycle(session: Session) -> Dict[str, int]:
    """
    Edge-triggered:
    - met = should_trigger(...)
    - fire ONLY if met==True AND last_met != True AND cooldown ok
    - set last_met=True on success
    - set last_met=False όταν δεν ισχύει η συνθήκη
    """
    counters = {"evaluated": 0, "triggered": 0, "errors": 0, "downgraded": 0}
    counters["downgraded"] = downgrade_expired_premiums(session)

    alerts = session.execute(select(Alert).where(Alert.enabled == True)).scalars().all()

    for alert in alerts:
        counters["evaluated"] += 1
        price = fetch_price_binance(alert.symbol)
        if price is None:
            counters["errors"] += 1
            print({"msg": "price_fetch_failed", "alert_id": alert.id, "symbol": alert.symbol})
            continue

        met = should_trigger(alert.rule, alert.value, price)
        last_met = getattr(alert, "last_met", None)  # ✅ backward-safe

        if not met:
            if last_met is not False:
                setattr(alert, "last_met", False)
                session.add(alert)
            continue

        already_met = (last_met is True)
        if not already_met and can_fire(alert.last_fired_at, alert.cooldown_seconds):
            chat_id = None
            try:
                if alert.user and alert.user.telegram_id:
                    chat_id = str(alert.user.telegram_id)
            except Exception:
                chat_id = None
            if not chat_id:
                chat_id = TELEGRAM_CHAT_ID

            txt = f"🔔 Alert #{alert.id} | {alert.symbol} {alert.rule} {alert.value} | price={price:.6f}"
            ok, status, body = _telegram_send(txt, chat_id)
            if ok:
                alert.last_fired_at = datetime.utcnow()
                setattr(alert, "last_met", True)
                session.add(alert)
                counters["triggered"] += 1
                print({"msg": "alert_sent", "alert_id": alert.id, "chat_id": chat_id, "status": status})
            else:
                counters["errors"] += 1
                print({"msg": "alert_send_failed", "alert_id": alert.id, "chat_id": chat_id, "status": status, "body": body})

    return counters
