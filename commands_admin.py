# commands_admin.py
from __future__ import annotations
import os
from typing import Set
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import text

from db import session_scope

def _admin_ids_from_env() -> Set[str]:
    return {s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()}

async def _admin_only(update: Update, admin_ids: Set[str]) -> bool:
    uid = str(update.effective_user.id)
    if uid not in admin_ids:
        await (update.message or update.effective_message).reply_text("Admin only.")
        return False
    return True

# ====== Existing admin handlers of yours can remain here ======
# (Δεν τα αλλάζω — συνέχισε να τα έχεις όπως ήταν.)

# ====== Trial management ======
def _subscriptions_columns(session) -> set[str]:
    cols = session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='subscriptions'")).scalars().all()
    return {c.lower() for c in cols}

def _insert_trial_row(session, user_id: int, expiry_iso: str) -> None:
    cols = _subscriptions_columns(session)
    p = {"uid": user_id, "expiry": expiry_iso}
    if "provider_status" in cols and "status_internal" in cols:
        session.execute(text(
            "INSERT INTO subscriptions (user_id, provider, provider_sub_id, provider_status, status_internal, created_at, updated_at) "
            "VALUES (:uid, 'trial', :expiry, 'active', 'active', NOW(), NOW())"
        ), p)
    elif "provider_status" in cols:
        session.execute(text(
            "INSERT INTO subscriptions (user_id, provider, provider_sub_id, provider_status, created_at, updated_at) "
            "VALUES (:uid, 'trial', :expiry, 'active', NOW(), NOW())"
        ), p)
    elif "status_internal" in cols:
        session.execute(text(
            "INSERT INTO subscriptions (user_id, provider, provider_sub_id, status_internal, created_at, updated_at) "
            "VALUES (:uid, 'trial', :expiry, 'active', NOW(), NOW())"
        ), p)
    else:
        session.execute(text(
            "INSERT INTO subscriptions (user_id, provider, provider_sub_id, created_at, updated_at) "
            "VALUES (:uid, 'trial', :expiry, NOW(), NOW())"
        ), p)

async def grantdays(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    args = context.args or []
    if len(args) < 2:
        await (update.message or update.effective_message).reply_text("Usage: /grantdays <telegram_id> <days>")
        return
    target_tg = args[0]
    try:
        days = int(args[1])
    except Exception:
        await (update.message or update.effective_message).reply_text("Days must be an integer.")
        return
    with session_scope() as s:
        u = s.execute(text("SELECT id FROM users WHERE telegram_id=:tg"), {"tg": target_tg}).mappings().first()
        if not u:
            await (update.message or update.effective_message).reply_text("User not found.")
            return
        uid = int(u["id"])
        now = datetime.utcnow()
        t = s.execute(text(
            "SELECT provider_sub_id FROM subscriptions WHERE user_id=:uid AND provider='trial' ORDER BY created_at DESC LIMIT 1"
        ), {"uid": uid}).mappings().first()
        base = now
        if t and t.get("provider_sub_id"):
            try:
                ex = datetime.fromisoformat(t.get("provider_sub_id"))
                if ex > now: base = ex
            except Exception:
                pass
        new_expiry = base + timedelta(days=days)
        _insert_trial_row(s, user_id=uid, expiry_iso=new_expiry.isoformat())
    await (update.message or update.effective_message).reply_text(
        f"Granted {days} day(s) to {target_tg} — expires {new_expiry.isoformat()} (UTC)."
    )

async def trialinfo(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    args = context.args or []
    if len(args) < 1:
        await (update.message or update.effective_message).reply_text("Usage: /trialinfo <telegram_id>")
        return
    target_tg = args[0]
    with session_scope() as s:
        row = s.execute(text(
            "SELECT provider_sub_id, created_at FROM subscriptions WHERE provider='trial' AND user_id=(SELECT id FROM users WHERE telegram_id=:tg) ORDER BY created_at DESC LIMIT 1"
        ), {"tg": target_tg}).mappings().first()
    if not row:
        await (update.message or update.effective_message).reply_text("No trial found for that user.")
    else:
        await (update.message or update.effective_message).reply_text(
            f"User {target_tg} — trial expires: {row['provider_sub_id']} — created: {row['created_at']}"
        )

async def listtrials(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT u.telegram_id, s.provider_sub_id, s.created_at FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE s.provider='trial' ORDER BY s.created_at DESC LIMIT 50"
        )).mappings().all()
    lines = ["<b>Recent trials</b>"]
    for r in rows:
        lines.append(f"{r['telegram_id']} — expires: {r['provider_sub_id']} — created: {r['created_at']}")
    await (update.message or update.effective_message).reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

def register_admin_handlers(app: Application, admin_ids: Set[str]):
    # κράτα όλα τα παλιά admin handlers σου εδώ... και πρόσθεσε:
    app.add_handler(CommandHandler("grantdays", lambda u, c: grantdays(u, c, admin_ids)))
    app.add_handler(CommandHandler("trialinfo", lambda u, c: trialinfo(u, c, admin_ids)))
    app.add_handler(CommandHandler("listtrials", lambda u, c: listtrials(u, c, admin_ids)))
