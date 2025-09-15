# features_market.py
# Market/utility helpers used by commands_extra.py
# - Fear & Greed
# - Funding rates (Binance futures)
# - Top movers (24h, Binance spot)
# - Quick chart URL helper
# - Crypto news from RSS (CoinDesk, Cointelegraph by default)

import os
import time
import math
import json
import html
import re
from typing import Optional, List, Tuple, Dict
from datetime import datetime, timezone

import requests
from xml.etree import ElementTree as ET

# ---------- HTTP ----------
_DEF_TIMEOUT = (10, 20)  # (connect, read)

def _http_get_json(url: str, params: dict | None = None, headers: dict | None = None) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, headers=headers, timeout=_DEF_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

def _http_get_text(url: str, params: dict | None = None, headers: dict | None = None) -> Optional[str]:
    try:
        r = requests.get(url, params=params, headers=headers, timeout=_DEF_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None

# ---------- Fear & Greed ----------
# Using alternative API with simple JSON response
def get_fear_greed() -> Optional[dict]:
    # Try alternative free source (feargreedindices.com mirror API)
    url = "https://api.alternative.me/fng/"
    data = _http_get_json(url, params={"limit": 1})
    if not data or "data" not in data or not data["data"]:
        return None
    d = data["data"][0]
    ts = None
    try:
        ts = datetime.fromtimestamp(int(d.get("timestamp", "0")), tz=timezone.utc).isoformat()
    except Exception:
        ts = None
    return {
        "value": d.get("value"),
        "value_classification": d.get("value_classification"),
        "timestamp": ts
    }

# ---------- Binance helpers ----------
_BINANCE_API = "https://api.binance.com"
_FAPI = "https://fapi.binance.com"  # futures

def _binance_24h_tickers() -> Optional[List[dict]]:
    url = f"{_BINANCE_API}/api/v3/ticker/24hr"
    data = _http_get_json(url)
    if not isinstance(data, list):
        return None
    return data

def _binance_funding_rate(symbol: Optional[str] = None) -> Optional[List[dict]]:
    url = f"{_FAPI}/fapi/v1/premiumIndex"
    params = {}
    if symbol:
        params["symbol"] = symbol.upper()
    data = _http_get_json(url, params=params)
    if not data:
        return None
    # API returns dict for one symbol, or list for all
    return data if isinstance(data, list) else [data]

# Symbol normalization
def _is_usdt_pair(sym: str) -> bool:
    return sym.endswith("USDT")

# External resolver can map BTC -> BTCUSDT, but provide a basic one here as fallback
def normalize_symbol(sym: str) -> Optional[str]:
    s = (sym or "").upper().strip()
    if not s:
        return None
    if s.endswith("USDT"):
        return s
    return s + "USDT"

# ---------- Funding (text output) ----------
def get_funding(symbol: Optional[str]) -> str:
    try:
        if symbol:
            s = normalize_symbol(symbol)
            rows = _binance_funding_rate(s)
            if not rows:
                return f"❌ Could not fetch funding for {symbol}."
            r = rows[0]
            fr = float(r.get("lastFundingRate", "0"))
            next_fund = r.get("nextFundingTime")
            nxt = "-"
            if next_fund:
                try:
                    ts = datetime.fromtimestamp(int(next_fund) / 1000, tz=timezone.utc)
                    nxt = ts.strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    pass
            return (f"📈 Funding {r.get('symbol','-')}\n"
                    f"Last: {fr*100:.4f}%\n"
                    f"Mark: {float(r.get('markPrice','0')):.6f}\n"
                    f"Next funding: {nxt}")
        else:
            # extremes: top +/- by abs funding
            rows = _binance_funding_rate(None)
            if not rows:
                return "No funding data right now."
            top = []
            for r in rows:
                try:
                    fr = abs(float(r.get("lastFundingRate", "0")))
                    sym = r.get("symbol", "")
                    if sym and _is_usdt_pair(sym):
                        top.append((sym, fr, float(r.get("lastFundingRate", "0"))))
                except Exception:
                    continue
            top.sort(key=lambda x: x[1], reverse=True)
            show = top[:10]
            lines = ["🏁 <b>Funding extremes</b> (abs)"]
            for sym, absv, raw in show:
                lines.append(f"• <code>{sym}</code>  {raw*100:.4f}%")
            return "\n".join(lines)
    except Exception as e:
        return f"Funding error: {e}"

# ---------- Top movers (24h) ----------
def get_top_movers(direction: str, limit: int = 10) -> List[Tuple[str, float]]:
    tickers = _binance_24h_tickers()
    if not tickers:
        return []
    rows = []
    for t in tickers:
        try:
            sym = t.get("symbol", "")
            if not _is_usdt_pair(sym):
                continue
            pct = float(t.get("priceChangePercent", "0"))
            if direction == "gainers" and pct > 0:
                rows.append((sym, pct))
            elif direction == "losers" and pct < 0:
                rows.append((sym, pct))
        except Exception:
            continue
    rows.sort(key=lambda x: x[1], reverse=(direction == "gainers"))
    return rows[:limit]

# ---------- Quick chart ----------
def make_quickchart_url(symbol: str) -> Optional[str]:
    # We will just produce a link to Binance 1h klines (24 points) rendered by quickchart.io
    # Simpler: return a TradingView mini chart link if you prefer. For now, return None if symbol invalid.
    sym = normalize_symbol(symbol)
    if not sym:
        return None
    # QuickView: provide a simple Binance symbol page as a fallback link
    return f"https://www.binance.com/en/trade/{sym[:-4]}_USDT"

# ---------- News (RSS) ----------
_DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss"
]

def _get_news_feeds() -> List[str]:
    env = (os.getenv("NEWS_FEEDS") or "").strip()
    if not env:
        return _DEFAULT_FEEDS[:]
    # comma or whitespace separated
    parts = [p.strip() for p in re.split(r"[,\s]+", env) if p.strip()]
    return parts or _DEFAULT_FEEDS[:]

def _parse_rss(xml_text: str) -> List[Tuple[str, str, Optional[str]]]:
    """
    Returns list of (title, link, summary)
    Supports RSS 2.0 and Atom basics.
    """
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    items: List[Tuple[str, str, Optional[str]]] = []

    # Try RSS 2.0
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if title and link:
            items.append((html.unescape(title), link, html.unescape(desc) if desc else None))

    # Try Atom
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//a:entry", ns):
            title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            href = ""
            for l in entry.findall("a:link", ns):
                href = l.get("href") or href
            summary = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
            if title and href:
                items.append((html.unescape(title), href, html.unescape(summary) if summary else None))

    return items

def get_news_headlines(limit: int = 5, keyword: Optional[str] = None) -> List[Tuple[str, str]]:
    """
    Fetch crypto news from RSS feeds. If keyword provided, filter by it.
    Returns list of (title, url).
    """
    feeds = _get_news_feeds()
    results: List[Tuple[str, str]] = []
    key = (keyword or "").lower().strip()

    for f in feeds:
        xml_text = _http_get_text(f)
        if not xml_text:
            continue
        items = _parse_rss(xml_text)
        for title, link, summary in items:
            if key and key not in title.lower() and (not summary or key not in summary.lower()):
                continue
            # Deduplicate by link
            if not any(link == r[1] for r in results):
                results.append((title, link))
            if len(results) >= max(50, limit):  # keep plenty; final trim happens below
                break

    # Final trim to requested limit
    return results[:limit]
