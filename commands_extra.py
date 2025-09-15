# commands_extra.py
# Extra commands for Crypto Alerts bot
# NOTE: /whale is disabled (temporary) and only returns a friendly message.

import os
from typing import Optional, List, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from features_market import (
    get_fear_greed,             # () -> Optional[dict]
    get_funding,                # (symbol: Optional[str]) -> str
    get_top_movers,             # (direction: str, limit: int = 10) -> List[Tuple[str,float]]
    make_quickchart_url,        # (symbol: str) -> Optional[str]
    get_news_headlines,         # (limit: int) -> List[Tuple[str, str]]
)
from models_extras import get_user_setting, set_user_setting  # user opt-ins for pump alerts


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

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    limit = 5
    if context.args:
        try:
            limit = max(1, min(15, int(context.args[0])))
        except Exception:
            pass
    items = get_news_headlines(limit)
    if not items:
        await (update.message or update.effective_message).reply_text("News not available right now."); return
    lines = ["🗞️ <b>Latest Crypto Headlines</b>"]
    for title, link in items:
        lines.append(f"• <a href=\"{link}\">{title}</a>")
    await (update.message or update.effective_message).reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=False)

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

# ---------- Disabled Whale ----------
async def cmd_whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await (update.message or update.effective_message).reply_text(
        "🐋 Whale alerts are temporarily disabled.\n"
        "We will enable this feature again once API access is available.",
        parse_mode=ParseMode.HTML
    )

# ---------- Register ----------
def register_extra_handlers(app: Application):
    app.add_handler(CommandHandler("feargreed", cmd_feargreed))
    app.add_handler(CommandHandler("funding", cmd_funding))
    app.add_handler(CommandHandler("topgainers", cmd_topgainers))
    app.add_handler(CommandHandler("toplosers", cmd_toplosers))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("dca", cmd_dca))
    app.add_handler(CommandHandler("pumplive", cmd_pumplive))
    app.add_handler(CommandHandler("whale", cmd_whale))  # still registered but disabled
