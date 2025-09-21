# commands_plus.py
from __future__ import annotations

import math
import re
import time
from typing import Dict, List, Tuple

import requests
from sqlalchemy import text
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from db import session_scope
from worker_logic import fetch_price_binance
from . import __name__ as _pkgname  # safe import if packaged; ignored when flat


BINANCE_TICKER_24H = "https://api.binance.com/api/v3/ticker/24hr"

def _ticker_24h(symbol_pair: str) -> dict | None:
    try:
        r = requests.get(BINANCE_TICKER_24H, params={"symbol": symbol_pair}, timeout=12)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def _num(x) -> float:
    try:
        return float(x)
    except Exception:
        return float("nan")

def _fmt(v: float, decimals: int = 2) -> str:
    if math.isnan(v):
        return "n/a"
    if abs(v) >= 1:
        return f"{v:.{decimals}f}"
    # more precision for very small prices
    return f"{v:.6f}"

def _guess_usdt_pair(symbol: str) -> str:
    s = (symbol or "").upper().strip()
    if s.endswith("USDT"):
        return s
    return f"{s}USDT"

# ────────────────────────────────────────────────────────────────────
# /dailyai [SYMBOLS...]

async def cmd_dailyai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lightweight AI-like daily summary using Binance 24h stats.
    Usage: /dailyai [BTC ETH SOL]
    """
    syms = [a.upper() for a in context.args] or ["BTC", "ETH", "SOL"]
    lines_gr = ["<b>📊 Ημερήσιο AI Insight</b>"]
    lines_en = ["<b>📊 Daily AI Insight</b>"]
    for s in syms[:8]:
        pair = _guess_usdt_pair(s)
        t = _ticker_24h(pair)
        if not t:
            lines_gr.append(f"• {s}: δεν βρέθηκε 24h στατιστικό.")
            lines_en.append(f"• {s}: 24h stats not found.")
            continue
        price = _num(t.get("lastPrice"))
        change_pct = _num(t.get("priceChangePercent"))
        vol = _num(t.get("volume"))
        hint_gr = "Σταθερό μοτίβο — ουδέτερη στάση." 
        hint_en = "Sideways pattern — neutral stance."
        if not math.isnan(change_pct):
            if change_pct >= 3:
                hint_gr = "Ανοδική ορμή • σκέψου σταδιακή κατοχύρωση κερδών."
                hint_en = "Bullish momentum • consider gradual profit taking."
            elif change_pct <= -3:
                hint_gr = "Πτωτική ορμή • σκέψου σταδιακές αγορές (DCA) αν πιστεύεις στο asset."
                hint_en = "Bearish momentum • consider staggered buys (DCA) if you believe in the asset."
        lines_gr.append(f"• {s}: τιμή { _fmt(price) } USDT • 24h { _fmt(change_pct) }% • vol { _fmt(vol,0) } — {hint_gr}")
        lines_en.append(f"• {s}: price { _fmt(price) } USDT • 24h { _fmt(change_pct) }% • vol { _fmt(vol,0) } — {hint_en}")
    msg = "\n".join(lines_gr + ["", "— — —", ""] + lines_en)
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ────────────────────────────────────────────────────────────────────
# /advisor <budget> <low|medium|high>

async def cmd_advisor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Simple robo-advisor allocation suggestion. No storage, no DB changes.
    Usage: /advisor 1000 low|medium|high
    """
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /advisor <budget> <low|medium|high>\nExample: /advisor 1000 medium"
        ); return
    try:
        budget = float(context.args[0])
    except Exception:
        await update.effective_message.reply_text("Bad budget. Use a number, e.g. 1000"); return
    risk = (context.args[1] or "").lower()
    if risk not in ("low", "medium", "high"):
        await update.effective_message.reply_text("Risk must be: low | medium | high"); return

    if risk == "low":
        alloc = [("BTC", 0.60), ("ETH", 0.30), ("Stable/Bluechip Alts", 0.10)]
        note_gr = "Στόχος: σταθερότητα, μικρό drawdown."
        note_en = "Goal: stability, smaller drawdown."
    elif risk == "medium":
        alloc = [("BTC", 0.50), ("ETH", 0.30), ("Quality Alts", 0.20)]
        note_gr = "Στόχος: ισορροπία ρίσκου/απόδοσης."
        note_en = "Goal: balanced risk/return."
    else:
        alloc = [("BTC", 0.40), ("ETH", 0.30), ("High-beta Alts", 0.30)]
        note_gr = "Στόχος: ανάπτυξη με υψηλότερη μεταβλητότητα."
        note_en = "Goal: growth with higher volatility."

    lines_gr = [f"<b>🤖 Προτεινόμενη κατανομή ({risk})</b>  για budget {budget:.2f}"]
    lines_en = [f"<b>🤖 Suggested allocation ({risk})</b>  for budget {budget:.2f}"]
    for name, w in alloc:
        amt = budget * w
        lines_gr.append(f"• {name}: {amt:.2f} ({int(w*100)}%)")
        lines_en.append(f"• {name}: {amt:.2f} ({int(w*100)}%)")

    lines_gr.append(f"💡 {note_gr}  |  Rebalance μηνιαία.")
    lines_en.append(f"💡 {note_en}  |  Rebalance monthly.")
    await update.effective_message.reply_text(
        "\n".join(lines_gr + ["", "— — —", ""] + lines_en),
        parse_mode=ParseMode.HTML
    )

# ────────────────────────────────────────────────────────────────────
# /whatif <SYMBOL> <long|short> <entry_price> [hours]

async def cmd_whatif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.effective_message.reply_text(
            "Usage: /whatif <SYMBOL> <long|short> <entry_price> [hours]\n"
            "Example: /whatif BTC long 68000 2"
        ); return
    sym = context.args[0].upper()
    side = context.args[1].lower()
    try:
        entry = float(context.args[2])
    except Exception:
        await update.effective_message.reply_text("Bad entry_price"); return
    hours = 0
    if len(context.args) >= 4:
        try:
            hours = int(context.args[3])
        except Exception:
            hours = 0

    pair = sym if sym.endswith("USDT") else f"{sym}USDT"
    price = fetch_price_binance(pair)
    if price is None:
        await update.effective_message.reply_text("Price fetch failed."); return

    move_pct = (price - entry) / entry * 100.0
    pnl_pct = move_pct if side == "long" else -move_pct
    gr = f"Τώρα {pair}={price:.6f}. Αν είχες {side} στο {entry:.6f}, PnL {pnl_pct:+.2f}%"
    en = f"Now {pair}={price:.6f}. If you were {side} at {entry:.6f}, PnL {pnl_pct:+.2f}%"
    if hours > 0:
        gr = f"{gr} ~{hours}h"
        en = f"{en} ~{hours}h"
    await update.effective_message.reply_text(gr + "\n" + en)

# ────────────────────────────────────────────────────────────────────
# /portfolio_sim <positions> <shock>
# positions: BTC:0.5,ETH:2,USDT:1000
# shock: BTC:-20,ETH:+5 (percent)

def _parse_kv_list(s: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k = k.strip().upper()
        try:
            out[k] = float(v.strip())
        except Exception:
            continue
    return out

async def cmd_portfolio_sim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /portfolio_sim <positions> <shock>\n"
            "Example: /portfolio_sim BTC:0.5,ETH:2,USDT:1000 BTC:-20,ETH:+5"
        ); return
    positions = _parse_kv_list(context.args[0])
    shocks = _parse_kv_list(context.args[1])
    if not positions:
        await update.effective_message.reply_text("No positions parsed."); return

    # Fetch prices
    values_now = 0.0
    values_shock = 0.0
    details = []
    for coin, qty in positions.items():
        if coin == "USDT":
            price_now = 1.0
        else:
            pair = coin if coin.endswith("USDT") else f"{coin}USDT"
            p = fetch_price_binance(pair)
            if p is None:
                await update.effective_message.reply_text(f"Price fetch failed for {coin}")
                return
            price_now = p
        val_now = qty * price_now
        shock_pct = shocks.get(coin, 0.0)
        val_after = val_now * (1.0 + shock_pct/100.0)
        values_now += val_now
        values_shock += val_after
        details.append((coin, qty, price_now, shock_pct, val_now, val_after))

    delta = values_shock - values_now
    delta_pct = (delta / values_now * 100.0) if values_now else 0.0

    lines_gr = ["<b>🧪 Προσομοίωση Χαρτοφυλακίου</b>"]
    lines_en = ["<b>🧪 Portfolio Simulation</b>"]
    for (coin, qty, pnow, spct, vnow, vafter) in details:
        lines_gr.append(f"• {coin}: qty {qty}, τιμή {pnow:.6f}, shock {spct:+.1f}% → {vafter:.2f} (από {vnow:.2f})")
        lines_en.append(f"• {coin}: qty {qty}, price {pnow:.6f}, shock {spct:+.1f}% → {vafter:.2f} (from {vnow:.2f})")
    lines_gr.append(f"\nΣύνολο τώρα: {values_now:.2f} → Με shock: {values_shock:.2f} ({delta:+.2f}, {delta_pct:+.2f}%)")
    lines_en.append(f"\nTotal now: {values_now:.2f} → With shock: {values_shock:.2f} ({delta:+.2f}, {delta_pct:+.2f}%)")

    await update.effective_message.reply_text(
        "\n".join(lines_gr + ["", "— — —", ""] + lines_en),
        parse_mode=ParseMode.HTML
    )

# ────────────────────────────────────────────────────────────────────
# /impactnews <headline...>  → heuristic score (0–100)

KEY_POS = ("approves", "approval", "etf", "integrates", "lists", "partnership", "upgrade", "merge", "reduce fees")
KEY_NEG = ("hack", "exploit", "ban", "suspend", "lawsuit", "criminal", "stablecoin depeg", "halt")

def _impact_score(headline: str) -> Tuple[int, str, str]:
    h = (headline or "").lower()
    score = 50
    reasons = []
    for kw in KEY_POS:
        if kw in h:
            score += 12
            reasons.append(f"+{kw}")
    for kw in KEY_NEG:
        if kw in h:
            score -= 15
            reasons.append(f"-{kw}")
    score = max(0, min(100, score))
    # Greek/English quick hints
    if score >= 80:
        gr = "Ισχυρό θετικό σήμα • πιθανή ανοδική κίνηση."
        en = "Strong positive signal • potential bullish move."
    elif score >= 60:
        gr = "Ήπια θετικό • παρακολούθηση για επιβεβαίωση."
        en = "Mild positive • monitor for confirmation."
    elif score <= 20:
        gr = "Υψηλός κίνδυνος • πιθανή πίεση τιμών."
        en = "High risk • possible price pressure."
    elif score <= 40:
        gr = "Αρνητικό/Προσοχή • περίμενε ξεκάθαρο σήμα."
        en = "Negative/Caution • wait for a clear signal."
    else:
        gr = "Ουδέτερο • πιθανό noise, ψάξε επιβεβαίωση."
        en = "Neutral • likely noise, look for confirmation."
    return score, gr, en

async def cmd_impactnews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headline = update.effective_message.text.partition(" ")[2].strip()
    if not headline:
        await update.effective_message.reply_text("Usage: /impactnews <headline>"); return
    score, gr, en = _impact_score(headline)
    msg = (
        f"<b>📰 Impact Score:</b> <b>{score}/100</b>\n"
        f"• GR: {gr}\n"
        f"• EN: {en}"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

# ────────────────────────────────────────────────────────────────────
# /topalertsboard → popular symbols by alert count

async def cmd_topalertsboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with session_scope() as s:
            rows = s.execute(text(
                "SELECT symbol, COUNT(*) AS c FROM alerts GROUP BY symbol ORDER BY c DESC LIMIT 10"
            )).all()
        if not rows:
            await update.effective_message.reply_text("No alerts found.")
            return
        lines = ["<b>🏆 Top Alerts Board</b>"]
        for r in rows:
            lines.append(f"• <code>{r.symbol}</code> → {r.c}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.effective_message.reply_text(f"Error: {e}")

# ────────────────────────────────────────────────────────────────────

def register_plus_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("dailyai", cmd_dailyai))
    app.add_handler(CommandHandler("advisor", cmd_advisor))
    app.add_handler(CommandHandler("whatif", cmd_whatif))
    app.add_handler(CommandHandler("portfolio_sim", cmd_portfolio_sim))
    app.add_handler(CommandHandler("impactnews", cmd_impactnews))
    app.add_handler(CommandHandler("topalertsboard", cmd_topalertsboard))
