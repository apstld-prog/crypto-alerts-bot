# commands_extra.py
# Extra commands for Crypto Alerts bot
# Includes: /feargreed, /funding, /topgainers, /toplosers, /chart, /news, /dca, /pumplive, /dailynews
# NOTE: /whale is disabled (temporary) and only returns a friendly message.

import os
from typing import Optional, List, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from features_market import (
    get_fear_greed,
    get_funding,
    get_top_movers,
    make_quickchart_url,
    get_news_headlines,
)
from models_extras import get_user_setting, set_user_setting
from plans import build_plan_info  # used for plan-aware /news

# ---------- Utilities ----------
def _reply_chunked(update: Update, text: str, limit: int = 3800):
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return
    s = text
    while s:
        part = s[:limit]
        s = s[limit:]
        msg.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ---------- Commands ----------
async def cmd_feargreed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_fear_greed()
    if not data:
        await (update.message or update.effective_message).reply_text("Fear & Greed not available right now.")
        return
    index_val = data.get("value")
    classification = data.get("value_classification")
    ts = data.get("timestamp")
    txt = f"🧭 <b>Fear &amp; Greed Index</b>\nValue: <b>{index_val}</b> ({classification})\n"
    if ts:
        txt += f"Updated: {ts}\n"
    await (update.message or update.effective_message).reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = (context.args[0].upper() if context.args else None)
    out = get_funding(symbol)
    _reply_chunked(update, out)

async def cmd_topgainers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_top_movers("gainers", limit=10)
    if not rows:
        await (update.message or update.effective_message).reply_text("No data right now."); return
    lines = ["📈 <b>Top Gainers (24h)</b>"]
    for sym, pct in rows:
        lines.append(f"• <code>{sym}</code>  +{pct:.2f}%")
    await (update.message or update.effective_message).reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_toplosers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_top_movers("losers", limit=10)
    if not rows:
        await (update.message or update.effective_message).reply_text("No data right now."); return
    lines = ["📉 <b>Top Losers (24h)</b>"]
    for sym, pct in rows:
        lines.append(f"• <code>{sym}</code>  {pct:.2f}%")
    await (update.message or update.effective_message).reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await (update.message or update.effective_message).reply_text("Usage: /chart <SYMBOL>\nExample: /chart BTC"); return
    symbol = context.args[0].upper()
    url = make_quickchart_url(symbol)
    if not url:
        await (update.message or update.effective_message).reply_text("Chart not available for this symbol right now."); return
    await (update.message or update.effective_message).reply_text(f"📊 <b>{symbol} 24h</b>\n{url}", parse_mode=ParseMode.HTML, disable_web_page_preview=False)

# ---- NEWS (plan-aware, keywords, limits) ----
async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # In this context we don't need admin set; build_plan_info will still return is_premium from DB
    plan = build_plan_info(user_id, set())

    # Args:
    # /news                 -> default limits per plan
    # /news 10              -> request 10 items
    # /news btc             -> keyword filter (default limit per plan)
    # /news btc 15          -> keyword + limit
    keyword: Optional[str] = None
    requested_limit: Optional[int] = None

    if context.args:
        if len(context.args) == 1:
            a0 = context.args[0]
            try:
                requested_limit = max(1, int(a0))
            except Exception:
                keyword = a0
        else:
            keyword = context.args[0]
            try:
                requested_limit = max(1, int(context.args[1]))
            except Exception:
                requested_limit = None

    # Plan-based limits
    if plan.has_unlimited:
        limit = requested_limit if requested_limit is not None else 10
        limit = max(1, min(30, limit))
    else:
        limit = 3

    items = get_news_headlines(limit=limit, keyword=keyword)
    if not items:
        await (update.message or update.effective_message).reply_text("News not available right now.")
        return

    title = "🗞️ <b>Latest Crypto Headlines</b>"
    if keyword:
        title = f"🗞️ <b>Crypto Headlines</b> — <i>{keyword.upper()}</i>"

    lines = [title]
    for t, link in items:
        safe_title = t.replace("\n", " ").strip()
        lines.append(f"• <a href=\"{link}\">{safe_title}</a>")

    await (update.message or update.effective_message).reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False
    )

async def cmd_dca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await (update.message or update.effective_message).reply_text("Usage: /dca <amount_per_buy> <buys> <symbol>\nExample: /dca 20 12 BTC"); return
    try:
        amt = float(context.args[0])
        n   = int(context.args[1])
        sym = context.args[2].upper()
    except Exception:
        await (update.message or update.effective_message).reply_text("Bad parameters. Example: /dca 20 12 BTC"); return
    total = amt * n
    await (update.message or update.effective_message).reply_text(f"🧮 <b>DCA</b>\nBuys: {n}\nPer buy: {amt}\nTotal: <b>{total}</b>\nSymbol: {sym}", parse_mode=ParseMode.HTML)

# Pump alerts opt-in using user_settings table via models_extras helpers
async def cmd_pumplive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.message or update.effective_message
    if not context.args:
        cur = get_user_setting(str(update.effective_user.id), "pump_optin") or "off"
        thr = get_user_setting(str(update.effective_user.id), "pump_threshold") or os.getenv("PUMP_THRESHOLD_PERCENT", "10")
        await chat.reply_text(f"Usage: /pumplive on|off [threshold%]\nCurrent: {cur}  threshold={thr}%")
        return

    action = context.args[0].lower()
    threshold = None
    if len(context.args) >= 2:
        try:
            threshold = max(1, min(50, int(float(context.args[1]))))
        except Exception:
            threshold = None

    uid = str(update.effective_user.id)
    if action == "on":
        set_user_setting(uid, "pump_optin", "on")
        if threshold is not None:
            set_user_setting(uid, "pump_threshold", str(threshold))
        await chat.reply_text(f"✅ Pump alerts ON{(' at ' + str(threshold) + '%') if threshold is not None else ''}.")
    elif action == "off":
        set_user_setting(uid, "pump_optin", "off")
        await chat.reply_text("✅ Pump alerts OFF.")
    else:
        await chat.reply_text("Usage: /pumplive on|off [threshold%]")

# ---- Daily news opt-in/out ----
async def cmd_dailynews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.message or update.effective_message
    uid = str(update.effective_user.id)

    if not context.args:
        cur = get_user_setting(uid, "dailynews") or "off"
        hour = os.getenv("DAILYNEWS_HOUR_UTC", "9")
        await chat.reply_text(f"Usage: /dailynews on|off\nCurrent: {cur}\nDelivery time: {hour}:00 UTC")
        return

    action = (context.args[0] or "").lower().strip()
    if action == "on":
        set_user_setting(uid, "dailynews", "on")
        await chat.reply_text("✅ Daily news enabled. You'll get a digest every day at 09:00 UTC.")
    elif action == "off":
        set_user_setting(uid, "dailynews", "off")
        await chat.reply_text("✅ Daily news disabled.")
    else:
        await chat.reply_text("Usage: /dailynews on|off")

# Whale disabled
async def cmd_whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await (update.message or update.effective_message).reply_text(
        "🐋 Whale alerts are temporarily disabled.\n"
        "We will enable this feature again once API access is available.",
        parse_mode=ParseMode.HTML
    )

# ---------- Registration ----------
def register_extra_handlers(app: Application):
    app.add_handler(CommandHandler("feargreed", cmd_feargreed))
    app.add_handler(CommandHandler("funding", cmd_funding))
    app.add_handler(CommandHandler("topgainers", cmd_topgainers))
    app.add_handler(CommandHandler("toplosers", cmd_toplosers))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("dca", cmd_dca))
    app.add_handler(CommandHandler("pumplive", cmd_pumplive))
    app.add_handler(CommandHandler("dailynews", cmd_dailynews))
    app.add_handler(CommandHandler("whale", cmd_whale))  # registered, but disabled
