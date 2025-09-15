
import os
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes
from telegram.constants import ParseMode
from sqlalchemy import select, text
from db import session_scope, User
from features_market import top_movers, funding_rate, fear_greed, klines_close_series, quickchart_url_from_series, fetch_news, whale_recent
from models_extras import SessionLocalExtra

def _msg_target(update: Update):
    return (update.message or (update.callback_query.message if update.callback_query else None))

async def cmd_feargreed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = fear_greed()
    if not d or not d.get("value"):
        await _msg_target(update).reply_text("Fear & Greed data not available right now.")
        return
    await _msg_target(update).reply_text(f"📊 Fear & Greed Index: {d['value']} — {d['classification']}")

async def cmd_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sym = (context.args[0].upper() if context.args else None)
    data = funding_rate(sym)
    if "error" in data:
        await _msg_target(update).reply_text("❌ " + data["error"]); return
    if sym:
        await _msg_target(update).reply_text(f"🧮 Funding {data['symbol']}: {data['funding']:.6f}")
    else:
        lines = ["🧲 Top |funding| extremes:"]
        for d in data["extremes"][:10]:
            lines.append(f"• {d['symbol']}: {d['lastFundingRate']:.6f}")
        await _msg_target(update).reply_text("\n".join(lines))

async def cmd_topgainers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gainers, losers = top_movers(10)
    g = "\n".join([f"• {r['symbol']}: {float(r['priceChangePercent']):+.2f}%" for r in gainers])
    await _msg_target(update).reply_text("🚀 Top 24h Gainers (USDT):\n" + g)

async def cmd_toplosers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gainers, losers = top_movers(10)
    l = "\n".join([f"• {r['symbol']}: {float(r['priceChangePercent']):+.2f}%" for r in losers])
    await _msg_target(update).reply_text("📉 Top 24h Losers (USDT):\n" + l)

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await _msg_target(update).reply_text("Usage: /chart <SYMBOL>  e.g. /chart BTC"); return
    sym = context.args[0].upper()
    pair = sym + "USDT"
    try:
        data = klines_close_series(pair, interval="1h", limit=24)
    except Exception as e:
        await _msg_target(update).reply_text(f"Chart data error: {e}"); return
    url = quickchart_url_from_series(data, title=f"{pair} (24h)")
    await _msg_target(update).reply_text(f"🖼️ Mini chart for {pair}:\n{url}", disable_web_page_preview=False)

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 5
    if context.args:
        try:
            n = max(1, min(15, int(context.args[0])))
        except Exception:
            pass
    items = fetch_news(n=n)
    if not items:
        await _msg_target(update).reply_text("News not available right now."); return
    lines = ["📰 Latest crypto headlines:"]
    for t, l in items:
        lines.append(f"• {t}\n{l}")
    await _msg_target(update).reply_text("\n\n".join(lines), disable_web_page_preview=False)

async def cmd_dca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await _msg_target(update).reply_text("Usage: /dca <amount_per_buy> <buys> <symbol>\nExample: /dca 20 12 BTC"); return
    try:
        amount = float(context.args[0]); buys = int(context.args[1]); sym = context.args[2].upper()
    except Exception:
        await _msg_target(update).reply_text("Bad parameters. Example: /dca 20 12 BTC"); return
    pair = sym + "USDT"
    try:
        closes = klines_close_series(pair, interval="1h", limit=1)
        price = closes[-1]
    except Exception:
        await _msg_target(update).reply_text("Price not available right now."); return
    invested = amount * buys
    est_qty = invested / price if price else 0.0
    await _msg_target(update).reply_text(
        f"🧮 DCA Plan for {sym}\n"
        f"• Buys: {buys} × {amount:.2f} = {invested:.2f} USDT\n"
        f"• Est. current qty at {price:.6f}: {est_qty:.6f} {sym}\n"
        f"(Note: This uses current price only for a quick estimate.)"
    )

# user settings: /pumplive on|off [threshold%]
async def cmd_pumplive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    if not context.args:
        await _msg_target(update).reply_text("Usage: /pumplive on|off [threshold_percent]"); return
    action = context.args[0].lower()
    thr = None
    if len(context.args) >= 2:
        try:
            thr = int(context.args[1])
        except Exception:
            thr = None
    with SessionLocalExtra() as s:
        row = s.execute(text("SELECT id FROM user_settings WHERE user_id=:uid"), {"uid": chat_id}).first()
        if action == "on":
            if row:
                s.execute(text("UPDATE user_settings SET pump_live=TRUE, pump_threshold_percent=:thr WHERE user_id=:uid"),
                          {"uid": chat_id, "thr": thr})
            else:
                s.execute(text("INSERT INTO user_settings (user_id, pump_live, pump_threshold_percent) VALUES (:uid, TRUE, :thr)"),
                          {"uid": chat_id, "thr": thr})
            s.commit()
            await _msg_target(update).reply_text(f"✅ Pump alerts enabled. Threshold: {thr or os.getenv('PUMP_THRESHOLD_PERCENT','10')}%")
        elif action == "off":
            if row:
                s.execute(text("UPDATE user_settings SET pump_live=FALSE WHERE user_id=:uid"), {"uid": chat_id})
                s.commit()
            await _msg_target(update).reply_text("🛑 Pump alerts disabled.")
        else:
            await _msg_target(update).reply_text("Usage: /pumplive on|off [threshold_percent]")

async def cmd_whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    min_usd = 250000
    if context.args:
        try:
            min_usd = int(context.args[0])
        except Exception:
            pass
    data = whale_recent(min_usd=min_usd)
    if "error" in data:
        await _msg_target(update).reply_text("ℹ️ " + data["error"]); return
    txs = (data.get("transactions") or [])[:10]
    if not txs:
        await _msg_target(update).reply_text("No recent whale transactions above your threshold."); return
    lines = [f"🐋 Recent whale tx (>{min_usd} USD):"]
    for t in txs:
        amt = t.get("amount_usd") or t.get("amount")
        sym = (t.get("symbol") or "").upper()
        src = (t.get("from") or {}).get("owner_type") or "unknown"
        dst = (t.get("to") or {}).get("owner_type") or "unknown"
        lines.append(f"• {sym} ~ ${amt:,.0f} — {src} → {dst}")
    await _msg_target(update).reply_text("\n".join(lines))

def register_extra_handlers(app):
    app.add_handler(CommandHandler("feargreed", cmd_feargreed))
    app.add_handler(CommandHandler("funding", cmd_funding))
    app.add_handler(CommandHandler("topgainers", cmd_topgainers))
    app.add_handler(CommandHandler("toplosers", cmd_toplosers))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("dca", cmd_dca))
    app.add_handler(CommandHandler("pumplive", cmd_pumplive))
    app.add_handler(CommandHandler("whale", cmd_whale))
