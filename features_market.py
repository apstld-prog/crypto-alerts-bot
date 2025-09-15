
import os
import math
import time
import json
import random
from typing import List, Dict, Optional, Tuple
import requests

USER_AGENT = {"User-Agent": "crypto-alerts-bot/extra-pack"}

BINANCE_SPOT_TICKER_24H = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_FAPI_PREMIUM_INDEX = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_SPOT_KLINES = "https://api.binance.com/api/v3/klines"
FNG_API = "https://api.alternative.me/fng/"

DEFAULT_NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss"
]

def _get_json(url: str, params: Optional[dict]=None, timeout: int=15):
    r = requests.get(url, params=params, headers=USER_AGENT, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _get_text(url: str, params: Optional[dict]=None, timeout: int=15):
    r = requests.get(url, params=params, headers=USER_AGENT, timeout=timeout)
    r.raise_for_status()
    return r.text

# ───── 24h Gainers / Losers (Binance spot) ─────
def top_movers(limit: int=10) -> Tuple[List[dict], List[dict]]:
    data = _get_json(BINANCE_SPOT_TICKER_24H)
    # Filter only USDT pairs and exclude leveraged tokens
    rows = [d for d in data if d.get("symbol","").endswith("USDT") and not d["symbol"].endswith(("UPUSDT","DOWNUSDT"))]
    for r in rows:
        try:
            r["priceChangePercent"] = float(r["priceChangePercent"])
        except Exception:
            r["priceChangePercent"] = 0.0
    rows.sort(key=lambda x: x["priceChangePercent"], reverse=True)
    gainers = rows[:limit]
    losers = rows[-limit:][::-1]
    return gainers, losers

# ───── Funding (Binance futures) ─────
def funding_rate(symbol: Optional[str]=None, extremes: int=10) -> Dict:
    data = _get_json(BINANCE_FAPI_PREMIUM_INDEX)
    for d in data:
        try:
            d["lastFundingRate"] = float(d.get("lastFundingRate") or 0.0)
        except Exception:
            d["lastFundingRate"] = 0.0
    if symbol:
        symbol = symbol.upper()
        hit = next((d for d in data if d.get("symbol")==f"{symbol}USDT"), None)
        if not hit:
            return {"error": f"Symbol {symbol} not found on Binance futures."}
        return {"symbol": symbol+"USDT", "funding": hit["lastFundingRate"], "time": hit.get("time")}
    # extremes by absolute funding
    data.sort(key=lambda x: abs(x["lastFundingRate"]), reverse=True)
    return {"extremes": data[:extremes]}

# ───── Fear & Greed ─────
def fear_greed() -> Dict:
    js = _get_json(FNG_API, params={"limit": 1, "format": "json"})
    d = (js.get("data") or [{}])[0]
    return {
        "value": d.get("value"),
        "classification": d.get("value_classification"),
        "timestamp": d.get("timestamp")
    }

# ───── Klines + QuickChart helper ─────
def klines_close_series(symbol_usdt: str, interval: str="1h", limit: int=24) -> List[float]:
    data = _get_json(BINANCE_SPOT_KLINES, params={"symbol": symbol_usdt, "interval": interval, "limit": limit})
    closes = [float(c[4]) for c in data]
    return closes

def quickchart_url_from_series(series: List[float], title: str="Price"):
    # Lightweight sparkline (no heavy local deps). Chart.js 4 via QuickChart.
    return (
        "https://quickchart.io/chart"
        "?c=" + json.dumps({
            "type": "line",
            "data": {"labels": list(range(len(series))), "datasets":[{"data": series}]},
            "options": {"plugins":{"legend":{"display":False},"title":{"display":True,"text":title}}}
        })
    )

# ───── RSS News (simple) ─────
def fetch_news(n: int=5) -> List[Tuple[str,str]]:
    # Very small RSS pull without external libs
    import re
    feeds = os.getenv("NEWS_FEEDS")
    if feeds:
        urls = [u.strip() for u in feeds.split(",") if u.strip()]
    else:
        urls = DEFAULT_NEWS_FEEDS
    items = []
    for url in urls:
        try:
            xml = _get_text(url, timeout=12)
            for m in re.finditer(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?</item>", xml, re.S|re.I):
                title = re.sub(r"<.*?>", "", m.group(1)).strip()
                link = re.sub(r"<.*?>", "", m.group(2)).strip()
                if title and link:
                    items.append((title, link))
        except Exception:
            continue
    # de-dup by title
    seen = set()
    uniq = []
    for t,l in items:
        if t in seen: 
            continue
        seen.add(t); uniq.append((t,l))
        if len(uniq) >= n:
            break
    return uniq

# ───── Quick "pump" detector (5m % change) ─────
def percent_change(a: float, b: float) -> float:
    if a == 0: 
        return 0.0
    return ((b - a) / a) * 100.0

def last_5m_change(symbol_usdt: str) -> Optional[float]:
    # use 1m klines, last 6 points (approx 5m change from t-5 to last close)
    data = _get_json(BINANCE_SPOT_KLINES, params={"symbol": symbol_usdt, "interval": "1m", "limit": 6})
    closes = [float(x[4]) for x in data]
    if len(closes) < 2:
        return None
    return percent_change(closes[0], closes[-1])

# ───── Whale Alert (optional) ─────
def whale_recent(min_usd: int=250000) -> Dict:
    api_key = os.getenv("WHALE_ALERT_API_KEY")
    if not api_key:
        return {"error": "Missing WHALE_ALERT_API_KEY. Get one at https://docs.whale-alert.io/ and set env var."}
    url = "https://api.whale-alert.io/v1/transactions"
    params = {"api_key": api_key, "min_value": min_usd, "limit": 10}
    js = _get_json(url, params=params)
    return js or {}
