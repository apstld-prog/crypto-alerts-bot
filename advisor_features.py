# advisor_features.py
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import text

from db import session_scope
from worker_logic import fetch_price_binance

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADVISOR_HOUR_UTC = int(os.getenv("ADVISOR_HOUR_UTC", "9"))  # default: 09:00 UTC daily
ADVISOR_MIN_VOLUME_USDT = float(os.getenv("ADVISOR_MIN_VOLUME_USDT", "0"))  # optional gate


def _ensure_tables():
    with session_scope() as s:
        s.execute(text(
            """
            CREATE TABLE IF NOT EXISTS advisor_profiles (
                user_id BIGINT PRIMARY KEY,
                risk TEXT NOT NULL,
                budget NUMERIC NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        ))
        s.execute(text(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                telegram_id TEXT UNIQUE
            );
            """
        ))
        s.commit()


def _alloc_for(risk: str):
    r = (risk or "").lower()
    if r == "low":
        return [("BTC", 0.60), ("ETH", 0.30), ("ALTS", 0.10)]
    if r == "medium":
        return [("BTC", 0.50), ("ETH", 0.30), ("ALTS", 0.20)]
    return [("BTC", 0.40), ("ETH", 0.30), ("ALTS", 0.30)]


def _send_dm(telegram_id: str, html: str):
    if not BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": telegram_id,
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
    except Exception:
        pass


def _fetch_prices(symbols: list[str]) -> dict[str, float]:
    out = {}
    for s in symbols:
        pair = s if s.endswith("USDT") else f"{s}USDT"
        p = fetch_price_binance(pair)
        if p is not None:
            out[s.upper()] = float(p)
    return out


def build_rebalance_message_for_user(user_id: int) -> str | None:
    """Return HTML message for a user's advisor profile, or None if not set."""
    _ensure_tables()
    with session_scope() as s:
        row = s.execute(text(
            "SELECT risk, budget, u.telegram_id "
            "FROM advisor_profiles ap JOIN users u ON ap.user_id = u.id "
            "WHERE ap.user_id=:uid"
        ), {"uid": user_id}).first()
    if not row:
        return None

    risk = row.risk
    budget = float(row.budget)
    alloc = _alloc_for(risk)

    # Simple “over/underweight” check vs target weights using current prices
    # We only check BTC & ETH spot; ALTS acts as residual.
    prices = _fetch_prices(["BTC", "ETH"])
    btc_p = prices.get("BTC"); eth_p = prices.get("ETH")
    if btc_p is None or eth_p is None:
        # fallback textual message
        lines = [
            "<b>🤖 Advisor Daily Check</b>",
            f"Budget: {budget:.2f} | Risk: {risk}",
            "Prices unavailable now. Try again later.",
            "",
            "<i>Targets</i>:"
        ]
        for name, w in alloc:
            amt = budget * w
            lines.append(f"• {name}: {amt:.2f} ({int(w*100)}%)")
        return "\n".join(lines)

    # Compute current weights under hypothetical holdings equal to target (guidance-style)
    # and propose tilt changes if 24h change pushed a coin >10% away from target weight.
    # This is deliberately simple & stateless.
    tgt = {k: w for (k, w) in alloc}
    tgt_btc = tgt.get("BTC", 0.0); tgt_eth = tgt.get("ETH", 0.0); tgt_alts = tgt.get("ALTS", 0.0)
    btc_amt = budget * tgt_btc
    eth_amt = budget * tgt_eth
    alts_amt = budget * tgt_alts
    total_now = btc_amt + eth_amt + alts_amt
    if total_now <= 0:
        return None

    # “Rebalance suggestion”: if one of BTC/ETH deviates > 10% relative from target weight, nudge.
    # NOTE: For a smarter approach you could add 24h/7d performance context.
    cur_w_btc = (btc_amt / total_now) if total_now else 0.0
    cur_w_eth = (eth_amt / total_now) if total_now else 0.0

    def _tilt(cur, target):
        if target <= 0:
            return 0.0
        rel = (cur - target) / target
        return rel

    tilt_btc = _tilt(cur_w_btc, tgt_btc)
    tilt_eth = _tilt(cur_w_eth, tgt_eth)

    hints_gr = []
    hints_en = []
    TH = 0.10  # 10% relative deviation
    if tilt_btc >= TH:
        hints_gr.append("• Μείωσε BTC κατά ~5–10% προς στόχο.")
        hints_en.append("• Trim BTC by ~5–10% toward target.")
    elif tilt_btc <= -TH:
        hints_gr.append("• Αύξησε BTC κατά ~5–10% προς στόχο.")
        hints_en.append("• Add BTC by ~5–10% toward target.")
    if tilt_eth >= TH:
        hints_gr.append("• Μείωσε ETH κατά ~5–10% προς στόχο.")
        hints_en.append("• Trim ETH by ~5–10% toward target.")
    elif tilt_eth <= -TH:
        hints_gr.append("• Αύξησε ETH κατά ~5–10% προς στόχο.")
        hints_en.append("• Add ETH by ~5–10% toward target.")

    if not hints_gr:
        hints_gr = ["• Είσαι κοντά στους στόχους — καμία ενέργεια."]
        hints_en = ["• You’re near targets — no action."]

    lines = [
        "<b>🤖 Daily Advisor • Rebalance</b>",
        f"Budget {budget:.2f} | Risk {risk}",
        f"BTC ≈ {btc_p:.2f} • ETH ≈ {eth_p:.2f}",
        "",
        "<i>Στόχοι</i> / <i>Targets</i>:"
    ]
    for name, w in alloc:
        amt = budget * w
        lines.append(f"• {name}: {amt:.2f} ({int(w*100)}%)")
    lines += ["", "<b>Προτάσεις</b> / <b>Suggestions</b>:", *hints_gr, *["— — —"], *hints_en]
    return "\n".join(lines)


def advisor_scheduler_loop():
    _ensure_tables()
    print({"msg": "advisor_scheduler_started", "hour_utc": ADVISOR_HOUR_UTC})
    last_run_date = None
    while True:
        try:
            now = datetime.utcnow()
            if (now.hour == ADVISOR_HOUR_UTC) and (last_run_date != now.date()):
                # enumerate users with advisor profiles
                with session_scope() as s:
                    rows = s.execute(text(
                        "SELECT ap.user_id, u.telegram_id FROM advisor_profiles ap "
                        "JOIN users u ON ap.user_id = u.id"
                    )).all()
                for r in rows:
                    msg = build_rebalance_message_for_user(r.user_id)
                    if msg:
                        _send_dm(r.telegram_id, msg)
                last_run_date = now.date()
        except Exception as e:
            print({"msg": "advisor_scheduler_error", "error": str(e)})
        time.sleep(30)


def start_advisor_scheduler():
    import threading
    t = threading.Thread(target=advisor_scheduler_loop, daemon=True)
    t.start()
