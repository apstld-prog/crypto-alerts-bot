import os
from datetime import datetime, timedelta
from typing import Dict, Optional, List

import requests
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from db import Alert, User, Subscription

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # optional default/fallback

# Πολλά σύμβολα → Binance USDT pairs
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
    """Στέλνει plain text (χωρίς Markdown) ώστε να μην σπάει ποτέ."""
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

def downgrade_expired_premiums(session: Session) -> int:
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
            end_row = Subscription(
                user_id=u.id,
                provider="paypal",
                provider_status="EXPIRED",
                status_internal="CANCELLED",
                provider_ref=sub.provider_ref,
                current_period_end=sub.current_period_end,
            )
            session.add(end_row)
            changed += 1
    return changed

def run_alert_cycle(session: Session) -> Dict[str, int]:
    counters = {"evaluated": 0, "triggered": 0, "errors": 0, "downgraded": 0}
    counters["downgraded"] = downgrade_expired_premiums(session)
    alerts = session.execute(
        select(Alert).where(Alert.enabled == True)  # boolean OK με ORM
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
            # ➜ Στείλε στον ίδιο τον χρήστη (fallback στο TELEGRAM_CHAT_ID αν λείπει)
            chat_id = None
            try:
                if alert.user and alert.user.telegram_id:
                    chat_id = str(alert.user.telegram_id)
            except Exception:
                chat_id = None
            text = f"🔔 Alert #{alert.id} | {alert.symbol} {alert.rule} {alert.value} | price={price:.6f}"
            ok = notify_telegram(text, chat_id=chat_id)
            if ok:
                counters["triggered"] += 1
            else:
                counters["errors"] += 1
    return counters
