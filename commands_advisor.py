# commands_advisor.py
from __future__ import annotations

import math
from typing import Optional

from sqlalchemy import text
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from db import session_scope
from plans import build_plan_info


def _risk_ok(r: str) -> bool:
    return (r or "").lower() in ("low", "medium", "high")


def _alloc_for(risk: str):
    r = (risk or "").lower()
    if r == "low":
        return [("BTC", 0.60), ("ETH", 0.30), ("Stable/Bluechip Alts", 0.10)], (
            "Στόχος: σταθερότητα, μικρό drawdown.",
            "Goal: stability, smaller drawdown.",
        )
    if r == "medium":
        return [("BTC", 0.50), ("ETH", 0.30), ("Quality Alts", 0.20)], (
            "Στόχος: ισορροπία ρίσκου/απόδοσης.",
            "Goal: balanced risk/return.",
        )
    # high
    return [("BTC", 0.40), ("ETH", 0.30), ("High-beta Alts", 0.30)], (
        "Στόχος: ανάπτυξη με υψηλότερη μεταβλητότητα.",
        "Goal: growth with higher volatility.",
    )


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
        s.commit()


async def cmd_setadvisor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setadvisor <budget> <low|medium|high>
    Stores user's advisor profile for continuous monitoring & notifications.
    """
    _ensure_tables()
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /setadvisor <budget> <low|medium|high>\nExample: /setadvisor 1000 medium"
        ); return
    try:
        budget = float(context.args[0])
    except Exception:
        await update.effective_message.reply_text("Bad budget. Use a number, e.g. 1000"); return
    risk = (context.args[1] or "").lower()
    if not _risk_ok(risk):
        await update.effective_message.reply_text("Risk must be: low | medium | high"); return

    plan = build_plan_info(str(update.effective_user.id))
    with session_scope() as s:
        s.execute(text(
            """
            INSERT INTO advisor_profiles (user_id, risk, budget)
            VALUES (:uid, :risk, :budget)
            ON CONFLICT (user_id)
            DO UPDATE SET risk = EXCLUDED.risk, budget = EXCLUDED.budget, updated_at = NOW()
            """
        ), {"uid": plan.user_id, "risk": risk, "budget": budget})
        s.commit()

    alloc, (note_gr, note_en) = _alloc_for(risk)
    lines_gr = [f"<b>🤖 Καταχωρήθηκε προφίλ συμβούλου</b>  budget {budget:.2f}  ρίσκο {risk}"]
    lines_en = [f"<b>🤖 Advisor profile saved</b>  budget {budget:.2f}  risk {risk}"]
    for name, w in alloc:
        amt = budget * w
        lines_gr.append(f"• {name}: {amt:.2f} ({int(w*100)}%)")
        lines_en.append(f"• {name}: {amt:.2f} ({int(w*100)}%)")
    lines_gr.append(f"💡 {note_gr}  |  Rebalance ειδοποιήσεις: ON (καθημερινά)")
    lines_en.append(f"💡 {note_en}  |  Rebalance notifications: ON (daily)")

    await update.effective_message.reply_text(
        "\n".join(lines_gr + ["", "— — —", ""] + lines_en),
        parse_mode=ParseMode.HTML
    )


async def cmd_myadvisor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_tables()
    plan = build_plan_info(str(update.effective_user.id))
    with session_scope() as s:
        row = s.execute(text(
            "SELECT risk, budget, updated_at FROM advisor_profiles WHERE user_id=:uid"
        ), {"uid": plan.user_id}).first()
    if not row:
        await update.effective_message.reply_text(
            "No advisor profile saved. Use: /setadvisor <budget> <low|medium|high>"
        ); return

    alloc, (note_gr, note_en) = _alloc_for(row.risk)
    lines_gr = [f"<b>🤖 Προφίλ συμβούλου</b>  budget {float(row.budget):.2f}  ρίσκο {row.risk}"]
    lines_en = [f"<b>🤖 Advisor profile</b>  budget {float(row.budget):.2f}  risk {row.risk}"]
    for name, w in alloc:
        amt = float(row.budget) * w
        lines_gr.append(f"• {name}: {amt:.2f} ({int(w*100)}%)")
        lines_en.append(f"• {name}: {amt:.2f} ({int(w*100)}%)")
    lines_gr.append(f"Τελευταία ενημέρωση: {row.updated_at}")
    lines_en.append(f"Last updated: {row.updated_at}")
    await update.effective_message.reply_text(
        "\n".join(lines_gr + ["", "— — —", ""] + lines_en),
        parse_mode=ParseMode.HTML
    )


async def cmd_rebalance_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Force a manual check & suggestion for the current user (one-shot).
    This mirrors the scheduled daily advisor check but only for the caller.
    """
    from advisor_features import build_rebalance_message_for_user  # local import to avoid cycle
    plan = build_plan_info(str(update.effective_user.id))
    msg = build_rebalance_message_for_user(plan.user_id)
    if not msg:
        await update.effective_message.reply_text(
            "No advisor profile found. Set one with /setadvisor <budget> <low|medium|high>."
        ); return
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def register_advisor_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("setadvisor", cmd_setadvisor))
    app.add_handler(CommandHandler("myadvisor", cmd_myadvisor))
    app.add_handler(CommandHandler("rebalance_now", cmd_rebalance_now))
