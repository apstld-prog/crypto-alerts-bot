# worker_logic.py
# Alert evaluation & sending logic

import os
from datetime import datetime, timedelta
import requests
from sqlalchemy import text
from db import session_scope, engine

BINANCE_REST = "https://api.binance.com/api/v3/ticker/price"
ALERT_COOLDOWN_DEFAULT = 900  # seconds
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Γνωστά pairs (όπου έχει νόημα να τα χαρτογραφήσουμε ρητά)
SYMBOL_MAP = {
    # Top caps
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "ADA": "ADAUSDT",
    "DOGE": "DOGEUSDT",
    "LTC": "LTCUSDT",
    "BCH": "BCHUSDT",
    "TON": "TONUSDT",
    "LINK": "LINKUSDT",
    "MATIC": "MATICUSDT",  # (a.k.a POL)

    # Cosmos / Cosmos-like & γύρω απ’ αυτό (όσα υπάρχουν στο Binance)
    "ATOM": "ATOMUSDT",
    "OSMO": "OSMOUSDT",
    "INJ":  "INJUSDT",
    "DYDX": "DYDXUSDT",
    "SEI":  "SEIUSDT",
    "TIA":  "TIAUSDT",     # (Celestia)
    "RUNE": "RUNEUSDT",    # THORChain
    "KAVA": "KAVAUSDT",
    "AKT":  "AKTUSDT",     # Akash (διαθέσιμο στο Binance)
    # (Αν θες κι άλλα της Cosmos που δεν είναι στο Binance, θα αποτύχουν στο fetch — βλέπε fallback παρακάτω)

    # Άλλα δημοφιλή
    "AVAX": "AVAXUSDT",
    "DOT":  "DOTUSDT",
    "APT":  "APTUSDT",
    "ARB":  "ARBUSDT",
    "OP":   "OPUSDT",
    "SUI":  "SUIUSDT",
    "PEPE": "PEPEUSDT",
    "SHIB": "SHIBUSDT",
    "ARKM": "ARKMUSDT",
}

def fetch_price_binance(symbol: str) -> float | None:
    try:
        r = requests.get(BINANCE_REST, params={"symbol": symbol}, timeout=10)
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception:
        pass
    return None

def resolve_symbol(sym: str | None) -> str | None:
    """
    Επιστρέφει το Binance pair (π.χ. BTCUSDT).
    - Αν δοθεί ήδη σε μορφή *_USDT*, το δέχεται.
    - Αν υπάρχει στον χάρτη, επιστρέφει τον χάρτη.
    - Αλλιώς δοκιμάζει fallback: <SYM>USDT και *αν* παίρνει τιμή από Binance, το κρατάει.
    """
    if not sym:
        return None
    s = sym.upper().replace("/", "").strip()
    if s.endswith("USDT"):
        return s
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    candidate = f"{s}USDT"
    if fetch_price_binance(candidate) is not None:
        return candidate
    return None

def _should_fire(rule: str, value: float, price: float) -> bool:
    return (price > value) if rule == "price_above" else (price < value)

def _send_alert_message(tg_id: str, seq: int, symbol: str, rule: str, value: float, price: float, alert_id: int):
    op = ">" if rule == "price_above" else "<"
    text_msg = (
        f"🔔 Alert <b>A{seq}</b>\n"
        f"Symbol: <b>{symbol}</b>\n"
        f"Rule: {op} {value}\n"
        f"Price: <b>{price}</b>\n"
        f"Time: {datetime.utcnow().isoformat(timespec='seconds')}Z"
    )
    kb = {
        "inline_keyboard": [[
            {"text": "✅ Keep", "callback_data": f"ack:keep:{alert_id}"},
            {"text": "🗑️ Delete", "callback_data": f"ack:del:{alert_id}"},
        ]]
    }
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": tg_id, "text": text_msg, "parse_mode": "HTML",
                  "disable_web_page_preview": True, "reply_markup": kb},
            timeout=15,
        )
        # DEBUG: να βλέπουμε ότι στάλθηκε
        print({"msg":"send_alert_message", "chat_id": tg_id, "status": r.status_code, "body": r.text[:120]})
    except Exception as e:
        print({"msg":"send_alert_exception", "error": str(e)})

def resolve_price_for_alert(sym: str) -> float | None:
    return fetch_price_binance(sym)

def run_alert_cycle(session) -> dict:
    evaluated = triggered = errors = 0

    rows = session.execute(text("""
        SELECT a.id, a.user_id, a.user_seq, a.symbol, a.rule, a.value, a.cooldown_seconds,
               a.last_fired_at, a.last_met, u.telegram_id
        FROM alerts a
        LEFT JOIN users u ON u.id = a.user_id
        WHERE a.enabled = TRUE
        ORDER BY a.id ASC
        LIMIT 500
    """)).all()

    if not rows:
        return {"evaluated": 0, "triggered": 0, "errors": 0}

    symbols = sorted({r.symbol for r in rows})
    prices = {sym: resolve_price_for_alert(sym) for sym in symbols}
    now = datetime.utcnow()

    for r in rows:
        evaluated += 1
        price = prices.get(r.symbol)
        if price is None:
            continue
        try:
            meet = _should_fire(r.rule, float(r.value), float(price))
            cooldown = int(r.cooldown_seconds or ALERT_COOLDOWN_DEFAULT)
            can_fire = True
            if r.last_fired_at:
                if (now - r.last_fired_at) < timedelta(seconds=cooldown):
                    can_fire = False

            session.execute(text("UPDATE alerts SET last_met = :met WHERE id = :id"),
                            {"met": bool(meet), "id": r.id})

            if meet and can_fire and r.telegram_id:
                _send_alert_message(
                    tg_id=str(r.telegram_id),
                    seq=int(r.user_seq) if r.user_seq is not None else int(r.id),
                    symbol=r.symbol, rule=r.rule, value=float(r.value),
                    price=float(price), alert_id=int(r.id),
                )
                session.execute(text("UPDATE alerts SET last_fired_at = NOW() WHERE id = :id"),
                                {"id": r.id})
                triggered += 1

        except Exception:
            errors += 1

    return {"evaluated": evaluated, "triggered": triggered, "errors": errors}
