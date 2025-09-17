# altcoins_info.py
from typing import Dict, List, Tuple, Optional

TokenInfo = Dict[str, object]

def _info(
    symbol: str,
    name: str,
    summary: str,
    links: List[Tuple[str, str]],
    *,
    category: str = "offbinance",  # offbinance | presale | community
    risk_level: str = "High",
    pump_dump_warning: bool = True,
) -> TokenInfo:
    risk_line = f"\n⚠️ Risk: {risk_level} volatility" if risk_level else ""
    pd_line = (
        "\n🚨 Pump & dump caution: early/illiquid markets can move violently. "
        "Use small size & set alerts."
        if pump_dump_warning else ""
    )
    note = (summary.strip() + risk_line + pd_line).strip()
    return {
        "symbol": symbol.upper(),
        "name": name.strip(),
        "note": note,
        "links": links,
        "risk_level": risk_level,
        "pump_dump_warning": pump_dump_warning,
        "category": category,
    }

TOKENS: Dict[str, TokenInfo] = {
    "HYPER": _info(
        "HYPER",
        "Bitcoin Hyper",
        "Community-driven token focused on Bitcoin-themed virality and meme momentum.",
        links=[
            ("Website", "https://example.com/bitcoin-hyper"),
            ("X (Twitter)", "https://twitter.com/search?q=Bitcoin%20Hyper"),
            ("DexScreener", "https://dexscreener.com/"),
        ],
        category="offbinance",
    ),
    "OZ": _info(
        "OZ",
        "Ozak AI",
        "Early AI-themed token; positioning around AI assistants & tools.",
        links=[
            ("Website", "https://example.com/ozakai"),
            ("X (Twitter)", "https://twitter.com/search?q=Ozak%20AI"),
            ("DexScreener", "https://dexscreener.com/"),
        ],
        category="offbinance",
    ),
    "CATAI": _info(
        "CATAI", "CatAI",
        "Meme + AI narrative crossover. Community-led; liquidity may be thin.",
        links=[("X (Twitter)", "https://twitter.com/search?q=CatAI"),
               ("DexScreener", "https://dexscreener.com/")],
        category="community",
    ),
    "GAMER": _info(
        "GAMER", "GamerFi",
        "Gaming-oriented token (guilds/quests). Verify contract and liquidity.",
        links=[("X (Twitter)", "https://twitter.com/search?q=GamerFi"),
               ("DexScreener", "https://dexscreener.com/")],
        category="community",
    ),
    "MEME2": _info(
        "MEME2", "Meme 2.0",
        "High-beta meme play. Purely speculative.",
        links=[("X (Twitter)", "https://twitter.com/search?q=Meme%202.0"),
               ("DexScreener", "https://dexscreener.com/")],
        category="community",
    ),
    # Presales (placeholders — βάλε τα δικά σου πραγματικά links/ονόματα)
    "PRESAI": _info(
        "PRESAI", "Presale AI",
        "AI-narrative token currently in presale; verify contracts & vesting.",
        links=[("Site / Presale", "https://example.com/presale-ai"),
               ("Docs", "https://example.com/presale-ai/docs"),
               ("Twitter", "https://twitter.com/search?q=Presale%20AI")],
        category="presale", risk_level="Very High",
    ),
    "PREMEME": _info(
        "PREMEME", "Presale Meme",
        "Meme presale. Extremely high risk; watch unlock schedules.",
        links=[("Site / Presale", "https://example.com/presale-meme"),
               ("Twitter", "https://twitter.com/search?q=Presale%20Meme")],
        category="presale", risk_level="Very High",
    ),
}

def list_off_binance() -> List[str]:
    syms = [s for s, info in TOKENS.items()
            if info.get("category") in ("offbinance", "community", "presale")]
    return sorted(set(syms))

def list_presales() -> List[str]:
    return sorted([s for s, info in TOKENS.items() if info.get("category") == "presale"])

def get_off_binance_info(symbol: str) -> Optional[TokenInfo]:
    return TOKENS.get((symbol or "").upper())
