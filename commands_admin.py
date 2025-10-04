# commands_admin.py
from __future__ import annotations
import os
import time
from typing import Set, Iterable
from datetime import datetime, timedelta

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import text

from db import session_scope

# Helpers
def _admin_ids_from_env() -> Set[str]:
    return {s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()}

async def _admin_only(update: Update, admin_ids: Set[str]) -> bool:
    uid = str(update.effective_user.id)
    if uid not in admin_ids:
        await (update.message or update.effective_message).reply_text("Admin only.")
        return False
    return True

# ============== Core admin utilities ==============

async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    with session_scope() as s:
        users = s.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
        premium = s.execute(text("SELECT COUNT(*) FROM users WHERE is_premium=TRUE")).scalar() or 0
        alerts = s.execute(text("SELECT COUNT(*) FROM alerts")).scalar() or 0
    await (update.message or update.effective_message).reply_text(
        f"👥 Users: {users}\n💎 Premium: {premium}\n🔔 Alerts: {alerts}"
    )

async def adminalerts(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    with session_scope() as s:
        total = s.execute(text("SELECT COUNT(*) FROM alerts")).scalar() or 0
        top = s.execute(text("""
            SELECT u.telegram_id, COUNT(*) AS c
            FROM alerts a JOIN users u ON u.id=a.user_id
            GROUP BY u.telegram_id ORDER BY c DESC LIMIT 10
        """)).mappings().all()
    lines = [f"🔔 Total alerts: {total}", "🏆 Top users:"]
    lines += [f"• {r['telegram_id']}: {r['c']}" for r in top] or ["(none)"]
    await (update.message or update.effective_message).reply_text("\n".join(lines))

async def adminusers(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT telegram_id, is_premium,
                   (SELECT COUNT(*) FROM alerts a WHERE a.user_id=u.id) AS alerts
            FROM users u ORDER BY u.id DESC LIMIT 50
        """)).mappings().all()
    lines = ["<b>Recent users</b>"]
    for r in rows:
        lines.append(f"{r['telegram_id']} — premium:{bool(r['is_premium'])} — alerts:{r['alerts']}")
    await (update.message or update.effective_message).reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def adminwho(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    args = context.args or []
    if not args:
        await (update.message or update.effective_message).reply_text("Usage: /adminwho <telegram_id>")
        return
    tgid = args[0]
    with session_scope() as s:
        u = s.execute(text("SELECT id, is_premium FROM users WHERE telegram_id=:tg"), {"tg": tgid}).mappings().first()
        if not u:
            await (update.message or update.effective_message).reply_text("User not found"); return
        uid = u["id"]; premium = bool(u["is_premium"])
        alerts = s.execute(text("SELECT COUNT(*) FROM alerts WHERE user_id=:uid"), {"uid": uid}).scalar() or 0
        trial = s.execute(text("""
            SELECT provider_sub_id FROM subscriptions WHERE user_id=:uid AND provider='trial'
            ORDER BY created_at DESC LIMIT 1
        """), {"uid": uid}).mappings().first()
    trial_exp = trial['provider_sub_id'] if trial else None
    await (update.message or update.effective_message).reply_text(
        f"User {tgid}\nPremium:{premium}\nAlerts:{alerts}\nTrial expires:{trial_exp}"
    )

async def adminplans(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    with session_scope() as s:
        total = s.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
        premium = s.execute(text("SELECT COUNT(*) FROM users WHERE is_premium=TRUE")).scalar() or 0
        active_trials = s.execute(text("""
            SELECT COUNT(*) FROM subscriptions s
            WHERE s.provider='trial' AND s.provider_sub_id::timestamp > NOW()
        """)).scalar() or 0
    await (update.message or update.effective_message).reply_text(
        f"Plans\nTotal users:{total}\nPremium:{premium}\nActive trials:{active_trials}"
    )

async def adminbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    msg = " ".join(context.args or [])
    if not msg:
        await (update.message or update.effective_message).reply_text("Usage: /adminbroadcast <message>")
        return
    sent = 0
    with session_scope() as s:
        ids = [r[0] for r in s.execute(text("SELECT telegram_id FROM users")).all()]
    for tgid in ids:
        try:
            await context.bot.send_message(chat_id=int(tgid), text=msg)
            sent += 1
            time.sleep(0.04)  # throttle
        except Exception:
            pass
    await (update.message or update.effective_message).reply_text(f"Broadcast sent to {sent}/{len(ids)} users.")

async def adminexec(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    sql = " ".join(context.args or [])
    if not sql or not sql.strip().lower().startswith("select"):
        await (update.message or update.effective_message).reply_text("Read-only. Usage: /adminexec <SELECT …>")
        return
    try:
        with session_scope() as s:
            rows = s.execute(text(sql)).mappings().all()
        if not rows:
            await (update.message or update.effective_message).reply_text("(no rows)")
            return
        # simple pretty
        keys = rows[0].keys()
        lines = [" | ".join(keys)]
        for r in rows[:50]:
            lines.append(" | ".join(str(r[k]) for k in keys))
        await (update.message or update.effective_message).reply_text("\n".join(lines))
    except Exception as e:
        await (update.message or update.effective_message).reply_text(f"Error: {e}")

async def adminhealth(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    base = os.getenv("WEB_URL") or ""
    try:
        b = requests.get(f"{base}/botok", timeout=5).json()
        a = requests.get(f"{base}/alertsok", timeout=5).json()
        await (update.message or update.effective_message).reply_text(
            f"botok: {b}\nalertsok: {a}"
        )
    except Exception as e:
        await (update.message or update.effective_message).reply_text(f"Error: {e}")

async def admintoken(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    tok = os.getenv("BOT_TOKEN") or ""
    masked = tok[:6] + "…" + tok[-4:] if len(tok) > 10 else "set"
    await (update.message or update.effective_message).reply_text(f"BOT_TOKEN: {masked}")

# ====== Trial helpers (grant/info/list) ======

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
        await (update.message or update.effective_message).reply_text("Days must be integer.")
        return
    with session_scope() as s:
        u = s.execute(text("SELECT id FROM users WHERE telegram_id=:tg"), {"tg": target_tg}).mappings().first()
        if not u:
            await (update.message or update.effective_message).reply_text("User not found."); return
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
    await (update.message or update.effective_message).reply_text(f"Granted {days}d to {target_tg}. New expiry: {new_expiry.isoformat()}")

async def trialinfo(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_ids: Set[str]):
    if not await _admin_only(update, admin_ids): return
    args = context.args or []
    if not args:
        await (update.message or update.effective_message).reply_text("Usage: /trialinfo <telegram_id>")
        return
    target_tg = args[0]
    with session_scope() as s:
        row = s.execute(text(
            "SELECT provider_sub_id, created_at FROM subscriptions WHERE provider='trial' AND user_id=(SELECT id FROM users WHERE telegram_id=:tg) ORDER BY created_at DESC LIMIT 1"
        ), {"tg": target_tg}).mappings().first()
    if not row:
        await (update.message or update.effective_message).reply_text("No trial found.")
    else:
        await (update.message or update.effective_message).reply_text(
            f"User {target_tg} — expires: {row['provider_sub_id']} — created: {row['created_at']}"
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

# ============== Registration ==============

def register_admin_handlers(app: Application, admin_ids: Set[str]):
    app.add_handler(CommandHandler("pumplive", lambda u, c: c.application.create_task(c.bot.send_message(u.effective_chat.id, "Use /pumplive via extra handlers if implemented."))))  # placeholder hook; kept compatibility
    app.add_handler(CommandHandler("adminstats", lambda u, c: adminstats(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminalerts", lambda u, c: adminalerts(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminusers", lambda u, c: adminusers(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminwho", lambda u, c: adminwho(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminplans", lambda u, c: adminplans(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminbroadcast", lambda u, c: adminbroadcast(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminexec", lambda u, c: adminexec(u, c, admin_ids)))
    app.add_handler(CommandHandler("adminhealth", lambda u, c: adminhealth(u, c, admin_ids)))
    app.add_handler(CommandHandler("admintoken", lambda u, c: admintoken(u, c, admin_ids)))

    # Trial management
    app.add_handler(CommandHandler("grantdays", lambda u, c: grantdays(u, c, admin_ids)))
    app.add_handler(CommandHandler("trialinfo", lambda u, c: trialinfo(u, c, admin_ids)))
    app.add_handler(CommandHandler("listtrials", lambda u, c: listtrials(u, c, admin_ids)))
