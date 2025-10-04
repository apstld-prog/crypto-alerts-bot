# commands_admin.py - admin utilities and trial management
from __future__ import annotations
import os
from typing import Set
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, Application
from sqlalchemy import text

from db import session_scope

ADMIN_KEY = os.getenv("ADMIN_KEY")

def _admin_ids_from_env() -> Set[str]:
    if not ADMIN_KEY:
        return set()
    return set([p.strip() for p in ADMIN_KEY.split(",") if p.strip()])

async def _admin_only(update: Update, admin_ids: Set[str]) -> bool:
    uid = str(update.effective_user.id)
    if uid not in admin_ids:
        await update.effective_message.reply_text("Admin only.")
        return False
    return True

# ----------------------- admin commands -----------------------

async def admin_grant_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /grantdays <telegram_id> <days>
    Grants (or extends) a trial for the target user by inserting a 'trial' subscription row
    whose provider_sub_id is the ISO datetime of expiry.
    """
    admin_ids = _admin_ids_from_env()
    if not await _admin_only(update, admin_ids):
        return
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text("Usage: /grantdays <telegram_id> <days>")
        return
    target_tg = args[0]
    try:
        days = int(args[1])
    except ValueError:
        await update.effective_message.reply_text("Days must be an integer.")
        return

    with session_scope() as session:
        user_row = session.execute(
            text("SELECT id FROM users WHERE telegram_id = :tg"),
            {"tg": target_tg}
        ).mappings().first()
        if not user_row:
            await update.effective_message.reply_text("User not found.")
            return

        uid = user_row["id"]
        now = datetime.now(timezone.utc)

        # If an active trial exists, extend from its expiry; otherwise, from now.
        trial_row = session.execute(
            text("""
                SELECT provider_sub_id
                FROM subscriptions
                WHERE user_id = :uid AND provider = 'trial'
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"uid": uid}
        ).mappings().first()

        base = now
        if trial_row and trial_row.get("provider_sub_id"):
            try:
                existing = datetime.fromisoformat(trial_row.get("provider_sub_id"))
                if existing > now:
                    base = existing
            except Exception:
                base = now

        new_expiry = base + timedelta(days=days)
        session.execute(
            text("""
                INSERT INTO subscriptions (user_id, provider, provider_sub_id, status_internal, created_at, updated_at)
                VALUES (:uid, 'trial', :expiry, 'active', NOW(), NOW())
            """),
            {"uid": uid, "expiry": new_expiry.isoformat()}
        )

    await update.effective_message.reply_text(
        f"Granted {days} day(s) trial to {target_tg}, expires {new_expiry.isoformat()} (UTC)."
    )

async def admin_list_trials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List recent trial subscriptions."""
    admin_ids = _admin_ids_from_env()
    if not await _admin_only(update, admin_ids):
        return

    with session_scope() as session:
        rows = session.execute(text("""
            SELECT u.telegram_id, s.provider_sub_id, s.created_at
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE s.provider = 'trial'
            ORDER BY s.created_at DESC
            LIMIT 50
        """)).mappings().all()

    lines = ["<b>Recent trials</b>"]
    for r in rows:
        lines.append(f"{r['telegram_id']} — expires: {r['provider_sub_id']} — created: {r['created_at']}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

async def admin_trial_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /trialinfo <telegram_id> — show trial info for a user"""
    admin_ids = _admin_ids_from_env()
    if not await _admin_only(update, admin_ids):
        return

    args = context.args or []
    if len(args) < 1:
        await update.effective_message.reply_text("Usage: /trialinfo <telegram_id>")
        return

    target_tg = args[0]
    with session_scope() as session:
        trial_row = session.execute(text("""
            SELECT provider_sub_id, created_at
            FROM subscriptions
            WHERE provider = 'trial'
              AND user_id = (SELECT id FROM users WHERE telegram_id = :tg)
            ORDER BY created_at DESC
            LIMIT 1
        """), {"tg": target_tg}).mappings().first()

    if not trial_row:
        await update.effective_message.reply_text("No trial found for that user.")
        return

    await update.effective_message.reply_text(
        f"User {target_tg} — trial expires: {trial_row['provider_sub_id']} — created: {trial_row['created_at']}"
    )

# -------------------- registration helpers --------------------

def register_admin_commands(app: Application):
    app.add_handler(CommandHandler("grantdays", admin_grant_days))
    app.add_handler(CommandHandler("listtrials", admin_list_trials))
    app.add_handler(CommandHandler("trialinfo", admin_trial_info))

# Backward-compat alias: server_combined.py imports register_admin_handlers
def register_admin_handlers(app: Application):
    return register_admin_commands(app)
