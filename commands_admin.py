# commands_admin.py
"""
Admin commands for the Crypto Alerts bot.

Usage (in private chat with the bot, only for admins):
  /adminstats                → quick stats (users, alerts, premium)
  /adminalerts               → count of alerts by status
  /adminusers                → last 15 users
  /adminwho <telegram_id>    → show info for a specific user
  /adminplans                → free vs premium breakdown
  /adminbroadcast <message>  → broadcast DM to all users (rate-limited)
  /adminexec <SQL>           → run a safe SELECT (read-only)
  /adminhealth               → read local /botok and /alertsok endpoints
  /admintoken                → show a masked BOT_TOKEN (last 6 chars)

All functions are defensive (work even if some tables/columns don't exist).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Iterable, Set

import requests
from sqlalchemy import text

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from db import session_scope
from plans import build_plan_info, plan_status_line


# -------------------------- helpers --------------------------

def _is_admin(tg_id: str | None, admin_ids: Set[str]) -> bool:
    return (tg_id or "") in admin_ids


async def _admin_only(update: Update, admin_ids: Set[str]) -> bool:
    uid = str(update.effective_user.id)
    if not _is_admin(uid, admin_ids):
        await update.effective_message.reply_text("Admin only.")
        return False
    return True


def _mask(s: str, keep: int = 6) -> str:
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def _try_scalar(sql: str, params: dict | None = None) -> int:
    try:
        with session_scope() as s:
            return int(s.execute(text(sql), params or {}).scalar() or 0)
    except Exception:
        return 0


# -------------------------- commands --------------------------

async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids):
        return

    users = _try_scalar("SELECT COUNT(*) FROM users")
    premiums = _try_scalar("SELECT COUNT(*) FROM users WHERE is_premium = TRUE")
    alerts = _try_scalar("SELECT COUNT(*) FROM alerts")
    alerts_on = _try_scalar("SELECT COUNT(*) FROM alerts WHERE enabled = TRUE")
    subs_active = 0
    try:
        subs_active = _try_scalar(
            "SELECT COUNT(*) FROM subscriptions WHERE status_internal IN "
            "('ACTIVE','CANCEL_AT_PERIOD_END')"
        )
    except Exception:
        subs_active = 0

    msg = (
        "<b>Admin Stats</b>\n"
        f"• Users: <b>{users}</b>\n"
        f"• Premium users (flag): <b>{premiums}</b>\n"
        f"• Active subscriptions: <b>{subs_active}</b>\n"
        f"• Alerts total: <b>{alerts}</b>\n"
        f"• Alerts ON: <b>{alerts_on}</b>\n"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)


async def adminalerts(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids):
        return

    by_user = []
    try:
        with session_scope() as s:
            rows = s.execute(text(
                "SELECT u.telegram_id, COUNT(a.id) AS c "
                "FROM users u LEFT JOIN alerts a ON a.user_id=u.id "
                "GROUP BY u.telegram_id ORDER BY c DESC NULLS LAST LIMIT 10"
            )).all()
            by_user = [(str(r.telegram_id), int(r.c or 0)) for r in rows]
    except Exception:
        by_user = []

    alerts = _try_scalar("SELECT COUNT(*) FROM alerts")
    alerts_on = _try_scalar("SELECT COUNT(*) FROM alerts WHERE enabled = TRUE")

    lines = [f"<b>Alerts</b>  total: <b>{alerts}</b> • ON: <b>{alerts_on}</b>"]
    if by_user:
        lines.append("\nTop users:")
        for tg, c in by_user:
            lines.append(f"• <code>{tg}</code> → {c}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def adminusers(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids):
        return

    lines = ["<b>Last users</b>"]
    try:
        with session_scope() as s:
            rows = s.execute(text(
                "SELECT id, telegram_id, is_premium, created_at "
                "FROM users ORDER BY id DESC LIMIT 15"
            )).all()
            for r in rows:
                created = getattr(r, "created_at", None)
                created_s = created.isoformat() + "Z" if created else "-"
                lines.append(
                    f"• #{r.id}  <code>{r.telegram_id}</code>  "
                    f"{'Premium' if r.is_premium else 'Free'}  {created_s}"
                )
    except Exception:
        lines.append("• (table 'users' not fully available)")

    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def adminwho(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /adminwho <telegram_id>")
        return
    qtg = context.args[0].strip()
    with session_scope() as s:
        row = s.execute(text("SELECT id, telegram_id, is_premium FROM users WHERE telegram_id=:tg"),
                        {"tg": qtg}).first()
        if not row:
            await update.effective_message.reply_text("User not found.")
            return
        uid = row.id
        plan_line = plan_status_line(build_plan_info(qtg, admin_ids))
        alerts = _try_scalar("SELECT COUNT(*) FROM alerts WHERE user_id=:u", {"u": uid})
        msg = (
            f"<b>User</b> #{uid}  <code>{qtg}</code>\n"
            f"Premium flag: {bool(row.is_premium)}\n"
            f"Alerts: {alerts}\n"
            f"{plan_line}"
        )
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)


async def adminplans(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids):
        return
    total = _try_scalar("SELECT COUNT(*) FROM users")
    premiums = _try_scalar("SELECT COUNT(*) FROM users WHERE is_premium = TRUE")
    free = max(0, total - premiums)
    await update.effective_message.reply_text(
        f"<b>Plans</b>\n• Free: <b>{free}</b>\n• Premium: <b>{premiums}</b>\n• Total: <b>{total}</b>",
        parse_mode=ParseMode.HTML,
    )


async def adminbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /adminbroadcast <message>")
        return
    message = update.effective_message.text.partition(" ")[2].strip()
    if not message:
        await update.effective_message.reply_text("Empty message.")
        return

    # gather recipients
    recips: list[str] = []
    try:
        with session_scope() as s:
            rows = s.execute(text("SELECT telegram_id FROM users ORDER BY id")).all()
            recips = [str(r.telegram_id) for r in rows if r.telegram_id]
    except Exception:
        recips = []

    if not recips:
        await update.effective_message.reply_text("No recipients.")
        return

    sent = 0
    failed = 0
    await update.effective_message.reply_text(f"Broadcast start: {len(recips)} users. This may take a while…")
    for tg in recips:
        try:
            await context.bot.send_message(chat_id=int(tg), text=message)
            sent += 1
        except Exception:
            failed += 1
        # small throttle to be gentle with rate limits
        await asyncio.sleep(0.05)

    await update.effective_message.reply_text(f"Broadcast done. Sent: {sent} • Failed: {failed}")


async def adminexec(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids):
        return
    sql = update.effective_message.text.partition(" ")[2].strip()
    if not sql:
        await update.effective_message.reply_text("Usage: /adminexec SELECT ...")
        return
    # guard: only SELECT allowed
    lowered = sql.strip().lower()
    if not lowered.startswith("select"):
        await update.effective_message.reply_text("Only SELECT is allowed.")
        return
    try:
        with session_scope() as s:
            rows = s.execute(text(sql)).fetchmany(20)
            if not rows:
                await update.effective_message.reply_text("(no rows)")
                return
            # simple text table
            headers = rows[0].keys()
            lines = [" | ".join(headers)]
            for r in rows:
                vals = [str(getattr(r, k)) for k in headers]
                lines.append(" | ".join(vals))
            out = "\n".join(lines)
            if len(out) > 3500:
                out = out[:3500] + "\n… (truncated)"
            await update.effective_message.reply_text(f"<pre>{out}</pre>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.effective_message.reply_text(f"SQL error: {e}")


async def adminhealth(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids):
        return
    port = int(os.getenv("PORT", "10000"))
    try:
        a = requests.get(f"http://127.0.0.1:{port}/alertsok", timeout=5).json()
        b = requests.get(f"http://127.0.0.1:{port}/botok", timeout=5).json()
        await update.effective_message.reply_text(
            f"<b>Health</b>\nAlerts: <code>{a}</code>\nBot: <code>{b}</code>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        await update.effective_message.reply_text(f"Health fetch error: {e}")


async def admintoken(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids):
        return
    tok = os.getenv("BOT_TOKEN") or ""
    await update.effective_message.reply_text(f"BOT_TOKEN = <code>{_mask(tok)}</code>", parse_mode=ParseMode.HTML)


# -------------------------- registration --------------------------

def register_admin_handlers(app: Application, admin_ids: Set[str]) -> None:
    """
    Wire up admin commands into the running Application.
    """
    app.add_handler(CommandHandler("adminstats", lambda u, c: adminstats(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminalerts", lambda u, c: adminalerts(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminusers", lambda u, c: adminusers(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminwho",   lambda u, c: adminwho(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminplans", lambda u, c: adminplans(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminbroadcast", lambda u, c: adminbroadcast(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminexec",  lambda u, c: adminexec(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminhealth",lambda u, c: adminhealth(u, c, admin_ids)))
    app.add_handler(CommandHandler("admintoken", lambda u, c: admintoken(u, c, admin_ids)))
    app.add_handler(CommandHandler("grantdays", lambda u, c: grantdays(u, c, admin_ids)))
    app.add_handler(CommandHandler("trialinfo", lambda u, c: trialinfo(u, c, admin_ids)))
    app.add_handler(CommandHandler("listtrials", lambda u, c: listtrials(u, c, admin_ids)))

# -------------------------- trial management (new) --------------------------
from datetime import datetime, timedelta, timezone

async def grantdays(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
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
        urow = session.execute(text("SELECT id FROM users WHERE telegram_id=:tg"), {"tg": target_tg}).mappings().first()
        if not urow:
            await update.effective_message.reply_text("User not found.")
            return
        uid = int(urow["id"])
        now = datetime.now(timezone.utc)
        trow = session.execute(text(
            "SELECT provider_sub_id FROM subscriptions WHERE user_id=:uid AND provider='trial' ORDER BY created_at DESC LIMIT 1"
        ), {"uid": uid}).mappings().first()
        base = now
        if trow and trow.get("provider_sub_id"):
            try:
                existing = datetime.fromisoformat(trow.get("provider_sub_id"))
                if existing > now:
                    base = existing
            except Exception:
                base = now
        new_expiry = base + timedelta(days=days)
        # adaptive insert depending on schema
        cols = session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='subscriptions'")).scalars().all()
        cols_lower = {c.lower() for c in cols}
        params = {"uid": uid, "expiry": new_expiry.isoformat()}
        if 'provider_status' in cols_lower and 'status_internal' in cols_lower:
            session.execute(text(
                "INSERT INTO subscriptions (user_id, provider, provider_sub_id, provider_status, status_internal, created_at, updated_at) "
                "VALUES (:uid, 'trial', :expiry, 'active', 'active', NOW(), NOW())"
            ), params)
        elif 'provider_status' in cols_lower:
            session.execute(text(
                "INSERT INTO subscriptions (user_id, provider, provider_sub_id, provider_status, created_at, updated_at) "
                "VALUES (:uid, 'trial', :expiry, 'active', NOW(), NOW())"
            ), params)
        elif 'status_internal' in cols_lower:
            session.execute(text(
                "INSERT INTO subscriptions (user_id, provider, provider_sub_id, status_internal, created_at, updated_at) "
                "VALUES (:uid, 'trial', :expiry, 'active', NOW(), NOW())"
            ), params)
        else:
            session.execute(text(
                "INSERT INTO subscriptions (user_id, provider, provider_sub_id, created_at, updated_at) "
                "VALUES (:uid, 'trial', :expiry, NOW(), NOW())"
            ), params)
    await update.effective_message.reply_text(f"Granted {days} day(s) to {target_tg} — expires {new_expiry.isoformat()} (UTC).")

async def trialinfo(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    args = context.args or []
    if len(args) < 1:
        await update.effective_message.reply_text("Usage: /trialinfo <telegram_id>")
        return
    target_tg = args[0]
    with session_scope() as session:
        row = session.execute(text(
            "SELECT provider_sub_id, created_at FROM subscriptions WHERE provider='trial' AND user_id=(SELECT id FROM users WHERE telegram_id=:tg) ORDER BY created_at DESC LIMIT 1"
        ), {"tg": target_tg}).mappings().first()
    if not row:
        await update.effective_message.reply_text("No trial found for that user.")
    else:
        await update.effective_message.reply_text(f"User {target_tg} — trial expires: {row['provider_sub_id']} — created: {row['created_at']}")

async def listtrials(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    with session_scope() as session:
        rows = session.execute(text(
            "SELECT u.telegram_id, s.provider_sub_id, s.created_at FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE s.provider='trial' ORDER BY s.created_at DESC LIMIT 50"
        )).mappings().all()
    lines = ["<b>Recent trials</b>"]
    for r in rows:
        lines.append(f"{r['telegram_id']} — expires: {r['provider_sub_id']} — created: {r['created_at']}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
