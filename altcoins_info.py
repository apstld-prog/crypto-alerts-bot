# altcoins_info.py
# Curated list of "Off-Binance" / Presale / Community tokens.
# Used to provide info links when a symbol has no Binance USDT pair.

from __future__ import annotations
from typing import Dict, List

# Keys are SYMBOL aliases that users would try in /price or /setalert
# IMPORTANT: These are NOT Binance pairs; they are info references only.
OFF_BINANCE_COINS: Dict[str, dict] = {
    # Bitcoin Hyper (presale/community)
    "HYPER": {
        "name": "Bitcoin Hyper",
        "links": [
            ("Website", "https://bitcoinhyper.com/"),
            ("Binance Square Tag", "https://www.binance.com/en/square/hashtag/bitcoinhyper"),
            ("DEXTools", "https://www.dextools.io/"),
        ],
        "note": "Not listed on Binance. Presale/community token.",
    },
    # Ozak AI (presale)
    "OZ": {
        "name": "Ozak AI",
        "links": [
            ("Website", "https://ozak.ai/"),
            ("CoinMarketCap", "https://coinmarketcap.com/currencies/ozak-ai/"),
            ("CoinGecko", "https://www.coingecko.com/en/coins/ozak-ai"),
            ("Binance Square Tag", "https://www.binance.com/en/square/hashtag/ozakai"),
        ],
        "note": "Presale token (no Binance pair yet).",
    },
    # --- Add more off-exchange coins below in the same format ---
    # "ABC": { "name":"Example Coin", "links":[("Website","https://..."), ...], "note":"..." },
}

def list_off_binance() -> List[str]:
    """Return sorted symbol list for /listalts."""
    return sorted(OFF_BINANCE_COINS.keys())

def get_off_binance_info(symbol: str) -> dict | None:
    """Return info dict for given alias symbol (uppercased)."""
    return OFF_BINANCE_COINS.get((symbol or "").upper().strip())
