# altcoins_info.py
from __future__ import annotations

from typing import Dict, List, Tuple, Optional

# Each entry:
#   "SYMBOL": {
#       "name": "...",
#       "note": "...",
#       "links": [("Title", "https://..."), ...],
#       "category": "offbinance" | "presale"
#   }

# Curated tokens (off-Binance/community) — add freely
_CURATED: Dict[str, Dict] = {
    # ==== Examples already discussed ====
    "HYPER": {
        "name": "Bitcoin Hyper",
        "note": "Narrative: BTC L2 / modular infra. High-volatility; community-driven. Not listed on Binance (auto-detect if listed later).",
        "links": [
            ("Website", "https://bitcoinhyper.org/"),
            ("Docs", "https://docs.bitcoinhyper.org/"),
            ("Twitter/X", "https://x.com/bitcoinhyper"),
            ("DexScreener", "https://dexscreener.com/"),
        ],
        "category": "offbinance",
    },
    "OZ": {
        "name": "Ozak AI",
        "note": "AI assistants/NLP tooling. Early-stage, watch liquidity & vesting.",
        "links": [
            ("Website", "https://ozakai.ai/"),
            ("Twitter/X", "https://x.com/ozakai_ai"),
            ("GitHub", "https://github.com/"),
        ],
        "category": "offbinance",
    },

    # ==== Add many more community/off-Binance tokens ====
    "SAGA": {
        "name": "Saga",
        "note": "App-chain infra. Monitor listings & liquidity depth.",
        "links": [("Website", "https://saga.xyz/"), ("Twitter/X", "https://x.com/Sagaxyz")],
        "category": "offbinance",
    },
    "ZK": {
        "name": "ZK Sync Ecosystem (Generic)",
        "note": "ZK-rollup ecosystem token placeholder. Verify exact ticker per venue.",
        "links": [("Website", "https://zksync.io/"), ("Docs", "https://docs.zksync.io/")],
        "category": "offbinance",
    },
    "MOVE": {
        "name": "Move-based Ecosystem (Aptos/Sui)",
        "note": "Builder tokens in Move VMs. Treat as theme basket.",
        "links": [("Aptos", "https://aptosfoundation.org/"), ("Sui", "https://sui.io/")],
        "category": "offbinance",
    },
    "DEPIN": {
        "name": "DePIN Basket",
        "note": "Decentralized physical infra narrative. High beta to cycles.",
        "links": [("Overview", "https://messari.io/")],
        "category": "offbinance",
    },

    # ==== Presales / Launch / IDO (HIGH RISK) ====
    "XYZ": {
        "name": "XYZ Presale",
        "note": "High risk presale (vesting/lockups). DYOR before any action.",
        "links": [("Landing", "https://example.com/xyz-presale")],
        "category": "presale",
    },
    "ALPHA": {
        "name": "Alpha Labs (Presale)",
        "note": "R&D / tooling project. Carefully check tokenomics.",
        "links": [("Site", "https://example.com/alpha")],
        "category": "presale",
    },
    "BETA": {
        "name": "Beta Network (Presale)",
        "note": "Infra presale; confirm launch venue & KYC.",
        "links": [("Site", "https://example.com/beta")],
        "category": "presale",
    },
    "GAMMA": {
        "name": "Gamma Games (Presale)",
        "note": "Gaming token. Marketing-heavy—watch unlocks.",
        "links": [("Site", "https://example.com/gamma")],
        "category": "presale",
    },
    "DELTA": {
        "name": "Delta AI (Presale)",
        "note": "AI x crypto presale. Ensure audit & security reviews.",
        "links": [("Site", "https://example.com/delta")],
        "category": "presale",
    },

    # You can continue growing the list:
    # "TOKEN": {...},
}


def get_off_binance_info(symbol: str) -> Optional[Dict]:
    if not symbol:
        return None
    return _CURATED.get(symbol.upper())


def list_off_binance() -> List[str]:
    return sorted([s for s, meta in _CURATED.items() if meta.get("category") == "offbinance"])


def list_presales() -> List[str]:
    return sorted([s for s, meta in _CURATED.items() if meta.get("category") == "presale"])


def all_symbols() -> List[str]:
    return sorted(list(_CURATED.keys()))
