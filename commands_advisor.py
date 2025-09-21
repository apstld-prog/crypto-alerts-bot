# commands_advisor.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Tuple, Optional

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from sqlalchemy import text

from db import session_scope
from plans import build_plan_info

# Try to reuse the project's Binance price utility if present
try:
    from worker_logic import fetch_price_binance  # expects pair like "BTCUSDT"
except Exception:
    fetch_price_binance = None  # fallback to raw HTTP below

BUDGET_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


@dataclass
class AdvisorProfile:
    user_id: int
    risk: str  # low | medium | high
    budget: float


def ensure_schema() -> None:
    """Create advisor_profiles table if missing."""
    with session_scope() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS advisor_profiles (
                user_id BIGINT PRIMARY KEY,
                risk TEXT NOT NULL,
                budget NUMERIC NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """))
        s.commit()


def risk_allocation(risk: str) -> Dict[str, float]:
    """
    Return allocation weights by risk level.
    Sum of values must be 1.0.
    """
    r = (risk or "medium").lower()
    if r == "low":
        return {"BTC": 0.70, "ETH": 0.20, "ALTS": 0.10}
    if r == "high":
        return {"BTC": 0.30, "ETH": 0.30, "ALTS": 0.40}
    # medium default
    return {"BTC": 0.50, "ETH": 0.30, "ALTS": 0.20}


# ───────────────────────── Live price helpers ─────────────────────────

_BINANCE_HOSTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]

def _http_get_json(url: str, timeout: float = 10.0) -> Optional[dict]:
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None

def get_spot_price_usdt(base: str) -> Optional[float]:
    """
    Return spot price for e.g. base='BTC' using pair BASEUSDT.
    Uses project's fetcher if available; otherwise direct Binance REST with fallbacks.
    """
    pair = f"{base.upper()}USDT"
    # Project utility
    if fetch_price_binance:
        try:
            p = fetch_price_binance(pair)
            if p is not None:
                return float(p)
        except Exception:
            pass
    # Raw HTTP fallbacks
    for host in _BINANCE_HOSTS:
        data = _http_get_json(f"{host}/api/v3/ticker/price?symbol={pair}")
        try:
            if data and "price" in data:
                return float(data["price"])
        except Exception:
            continue
    return None


def format_plan_with_live(budget: float, risk: str) -> Tuple[str, str]:
    """
    Return (full_html, alloc_only_html) including live units if prices are available.
    """
    weights = risk_allocation(risk)
    btc_p = get_spot_price_usdt("BTC")
    eth_p = get_spot_price_usdt("ETH")

    lines_header = [
        "👤 <b>Advisor Profile</b>",
        f"• Budget: <b>{budget:.2f}</b>",
        f"• Risk: <b>{risk}</b>",
        "",
        "<b>Suggested Allocation</b>:"
    ]

    def _alloc_line(asset: str, w: float) -> str:
        usd = budget * w
        if asset in {"BTC", "ETH"}:
            spot = btc_p if asset == "BTC" else eth_p
            if spot and spot > 0:
                units = usd / spot
                return f"• {asset}: <b>{int(w*100)}%</b>  (~{usd:.2f})  ≈ <b>{units:.6f} {asset}</b> @ {spot:.2f}"
        return f"• {asset}: <b>{int(w*100)}%</b>  (~{usd:.2f})"

    alloc_lines = [_alloc_line("BTC", weights.get("BTC", 0.0)),
                   _alloc_line("ETH", weights.get("ETH", 0.0)),
                   _alloc_line("ALTS", weights.get("ALTS", 0.0))]

    full = "\n".join(lines_header + alloc_lines)
    alloc_only = "\n".join(["<b>Suggested Allocation</b>:"] + alloc_lines)
    return full, alloc_only


# ───────────────────────── Commands ─────────────────────────

async def cmd_setadvisor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setadvisor <budget> <low|medium|high>
    """
    ensure_schema()
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /setadvisor <budget> <low|medium|high>\nExample: /setadvisor 1000 medium"
        )
        return

    budget_s = context.args[0].strip()
    risk = context.args[1].strip().lower()

    if not BUDGET_RE.match(budget_s):
        await update.effective_message.reply_text("Bad budget. Use a number, e.g. 1000")
        return
    if risk not in {"low", "medium", "high"}:
        await update.effective_message.reply_text("Bad risk. Use: low | medium | high")
        return

    budget = float(budget_s)
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, admin_ids=set())

    with session_scope() as s:
        s.execute(
            text("""
                INSERT INTO advisor_profiles (user_id, risk, budget)
                VALUES (:uid, :risk, :budget)
                ON CONFLICT (user_id)
                DO UPDATE SET risk=EXCLUDED.risk, budget=EXCLUDED.budget, updated_at=NOW();
            """),
            {"uid": plan.user_id, "risk": risk, "budget": budget},
        )
        s.commit()

    html, _ = format_plan_with_live(budget, risk)
    await update.effective_message.reply_text(
        f"✅ Saved.\n\n{html}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


async def cmd_myadvisor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_schema()
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, admin_ids=set())

    with session_scope() as s:
        row = s.execute(text("SELECT risk, budget FROM advisor_profiles WHERE user_id=:u"),
                        {"u": plan.user_id}).first()
    if not row:
        await update.effective_message.reply_text(
            "No advisor profile yet. Try: /setadvisor 1000 medium"
        )
        return

    html, _ = format_plan_with_live(float(row.budget), row.risk)
    await update.effective_message.reply_text(
        html, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_rebalance_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_schema()
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, admin_ids=set())

    with session_scope() as s:
        row = s.execute(text("SELECT risk, budget FROM advisor_profiles WHERE user_id=:u"),
                        {"u": plan.user_id}).first()
    if not row:
        await update.effective_message.reply_text(
            "No advisor profile yet. Try: /setadvisor 1000 medium"
        )
        return

    budget = float(row.budget)
    risk = row.risk
    _, only_alloc = format_plan_with_live(budget, risk)
    await update.effective_message.reply_text(
        f"🔁 <b>Rebalance Suggestion</b>\n{only_alloc}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


def register_advisor_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("setadvisor", cmd_setadvisor))
    app.add_handler(CommandHandler("myadvisor", cmd_myadvisor))
    app.add_handler(CommandHandler("rebalance_now", cmd_rebalance_now))
