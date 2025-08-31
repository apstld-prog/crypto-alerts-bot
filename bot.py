import os
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from db import init_db, session_scope, User

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in environment variables")

HELP_TEXT = (
    "🤖 *Crypto Alerts Bot*\n"
    "/start - register\n"
    "/stats - show bot stats\n"
    "/help - show this help\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id) if update.effective_user else None
    with session_scope() as session:
        user = session.query(User).filter(User.telegram_id == tg_id).one_or_none()
        if not user:
            user = User(telegram_id=tg_id, is_premium=False)
            session.add(user)
            session.flush()
    await update.message.reply_text("👋 Welcome! You're registered.\nUse /help for commands.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from sqlalchemy import text
    with session_scope() as session:
        users = session.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
        premium = session.execute(text("SELECT COUNT(*) FROM users WHERE is_premium = 1")).scalar_one()
        alerts = session.execute(text("""
            SELECT COUNT(*) FROM alerts
            WHERE enabled = 1 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
        """ )).scalar_one()
        subs = session.execute(text("""
            SELECT COUNT(*) FROM subscriptions WHERE status_internal IN ('ACTIVE')
        """ )).scalar_one()

    msg = (
        "📊 *Bot Stats*\n\n"
        f"👥 Users: {users}\n"
        f"💎 Premium users: {premium}\n"
        f"🔔 Active alerts: {alerts}\n"
        f"🧾 Subscriptions: ACTIVE={subs}\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))

    print({"msg": "bot_start"})
    app.run_polling()


if __name__ == "__main__":
    main()
