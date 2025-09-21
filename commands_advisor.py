# commands_advisor.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from sqlalchemy import text

from db import session_scope
from plans import build_plan_info

BUDGET_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


@dataclass
class AdvisorProfile:
    user_id: int
    risk: str  # low | medium | high
    budget: float


def ensure_schema():
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


def format_plan(budget: float, risk: str) -> Tuple[str, str]:
    weights = risk_allocation(risk)
    lines = [
        f"👤 <b>Advisor Profile</b>",
        f"• Budget: <b>{budget:.2f}</b>",
        f"• Risk: <b>{risk}</b>",
        "",
        "<b>Suggested Allocation</b>:"
    ]
    for k, w in weights.items():
        lines.append(f"• {k}: <b>{int(w*100)}%</b>  (~{budget*w:.2f})")
    return "\n".join(lines), "\n".join(lines[3:])


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

    html, _ = format_plan(budget, risk)
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

    html, _ = format_plan(float(row.budget), row.risk)
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
    _, only_alloc = format_plan(budget, risk)
    await update.effective_message.reply_text(
        f"🔁 <b>Rebalance Suggestion</b>\n{only_alloc}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


def register_advisor_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("setadvisor", cmd_setadvisor))
    app.add_handler(CommandHandler("myadvisor", cmd_myadvisor))
    app.add_handler(CommandHandler("rebalance_now", cmd_rebalance_now))
