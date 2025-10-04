import os
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from sqlalchemy import select, text

from db import init_db, session_scope, User, Subscription

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEB_URL = os.getenv("WEB_URL")
ADMIN_KEY = os.getenv("ADMIN_KEY")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in environment variables")

TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "10"))

HELP_TEXT = (
    "🤖 *Crypto Alerts Bot*\n"
    "/start - register and activate a free 10-day trial\n"
    "/help - show this help\n"
    "/stats - show bot stats\n"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    tg_id = str(tg_user.id) if tg_user else None
    with session_scope() as session:
        user = session.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
            session.add(user)
            session.flush()
            msg = "👋 Welcome! You've been registered."
        else:
            msg = "👋 Welcome back!"

        row = session.execute(
            text("SELECT id, created_at, provider_sub_id FROM subscriptions WHERE user_id = :uid AND provider = 'trial' ORDER BY created_at DESC LIMIT 1"),
            {"uid": user.id}
        ).mappings().first()

        now = datetime.now(timezone.utc)
        if row:
            expiry_iso = row.get("provider_sub_id")
            expiry = None
            try:
                if expiry_iso:
                    expiry = datetime.fromisoformat(expiry_iso)
            except Exception:
                expiry = None
            if expiry and expiry > now:
                days_left = (expiry - now).days
                msg += f"\n\n✅ You already have an active free trial for {days_left} more day(s)."
            else:
                msg += "\n\n⚠️ Your previous free trial has expired. To get more days, please contact the admin."
        else:
            expiry = now + timedelta(days=TRIAL_DAYS)
            session.execute(
                text("INSERT INTO subscriptions (user_id, provider, provider_sub_id, status_internal, created_at, updated_at) VALUES (:uid, 'trial', :expiry, 'active', NOW(), NOW())"),
                {"uid": user.id, "expiry": expiry.isoformat()}
            )
            msg += f"\n\n🎁 You received a free {TRIAL_DAYS}-day trial with full access. It will expire on {expiry.date().isoformat()} (UTC)."

    await update.message.reply_text(msg)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with session_scope() as session:
        users = session.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
        premium = session.execute(text("SELECT COUNT(*) FROM users WHERE is_premium = TRUE")).scalar_one()
        alerts = session.execute(text("SELECT COUNT(*) FROM alerts")).scalar_one()
    msg = (
        "📊 *Bot Stats*\n\n"
        f"👥 Users: {users}\n"
        f"💎 Premium users: {premium}\n"
        f"🔔 Total alerts: {alerts}\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.run_polling()

if __name__ == "__main__":
    main()
