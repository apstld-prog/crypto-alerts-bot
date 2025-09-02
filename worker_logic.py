
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import requests
from sqlalchemy import select, desc
from sqlalchemy.orm import Session
from db import Alert, User, Subscription

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL_MAP = {
    "BTC":"BTCUSDT","ETH":"ETHUSDT","BNB":"BNBUSDT","XRP":"XRPUSDT","ADA":"ADAUSDT","SOL":"SOLUSDT","DOGE":"DOGEUSDT",
    "TRX":"TRXUSDT","DOT":"DOTUSDT","MATIC":"MATICUSDT","LTC":"LTCUSDT","BCH":"BCHUSDT","LINK":"LINKUSDT","XLM":"XLMUSDT",
    "ATOM":"ATOMUSDT","AVAX":"AVAXUSDT","ETC":"ETCUSDT","XMR":"XMRUSDT","XTZ":"XTZUSDT","AAVE":"AAVEUSDT",
    "SHIB":"SHIBUSDT","PEPE":"PEPEUSDT","ARB":"ARBUSDT","OP":"OPUSDT","SUI":"SUIUSDT","APT":"APTUSDT",
    "INJ":"INJUSDT","RNDR":"RNDRUSDT","TIA":"TIAUSDT","SEI":"SEIUSDT","WIF":"WIFUSDT","JUP":"JUPUSDT",
    "PYTH":"PYTHUSDT","KAS":"KASUSDT","JASMY":"JASMYUSDT","IMX":"IMXUSDT"
}

def resolve_symbol(sym: str) -> Optional[str]:
    s = (sym or "").upper().replace("/", "").strip()
    if s.endswith("USDT"):
        return s
    return SYMBOL_MAP.get(s)

def fetch_price_binance(symbol: str, timeout: int = 8) -> Optional[float]:
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": symbol}, timeout=timeout)
        r.raise_for_status()
        return float(r.json()["price"])
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

def notify_telegram(text: str, chat_id: Optional[str] = None) -> bool:
    if not BOT_TOKEN:
        return False
    chat = chat_id or TELEGRAM_CHAT_ID
    if not chat:
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": chat, "text": text}, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def run_alert_cycle(session: Session) -> Dict[str,int]:
    counters = {"evaluated": 0, "triggered": 0}
    alerts = session.execute(select(Alert).where(Alert.enabled==True)).scalars().all()
    for alert in alerts:
        counters["evaluated"] += 1
        price = fetch_price_binance(alert.symbol)
        if price is None: continue
        if should_trigger(alert.rule, alert.value, price) and can_fire(alert.last_fired_at, alert.cooldown_seconds):
            alert.last_fired_at = datetime.utcnow()
            session.add(alert)
            session.flush()
            counters["triggered"] += 1
            notify_telegram(f"🔔 Alert {alert.symbol} {alert.rule} {alert.value} | price={price}")
    return counters
