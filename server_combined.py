# server_combined.py
from __future__ import annotations

import os
import re
import time
import threading
import html
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

import requests
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse, PlainTextResponse, JSONResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ApplicationHandlerStop
)

from sqlalchemy import text

# Local modules
from db import init_db, session_scope, engine
from worker_logic import run_alert_cycle, resolve_symbol, fetch_price_binance
from commands_extra import register_extra_handlers
from models_extras import init_extras
from plans import build_plan_info, can_create_alert, plan_status_line
from altcoins_info import get_off_binance_info, list_off_binance, list_presales
from commands_admin import register_admin_handlers
from commands_plus import register_plus_handlers
from commands_advisor import register_advisor_handlers
from advisor_features import start_advisor_scheduler
from feedback_followup import start_feedback_scheduler
from usage_limits import increment_and_check, DEFAULT_FREE_LIMIT

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
WEB_URL = (os.getenv("WEB_URL") or "").strip() or None

INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))
FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT", "10"))

PAYPAL_PLAN_ID = (os.getenv("PAYPAL_PLAN_ID") or "").strip() or None
PAYPAL_SUBSCRIBE_URL = (os.getenv("PAYPAL_SUBSCRIBE_URL") or "").strip() or None

RUN_BOT = os.getenv("RUN_BOT", "1") == "1"
RUN_ALERTS = os.getenv("RUN_ALERTS", "1") == "1"

_ADMIN_IDS = {s.strip() for s in (os.getenv("ADMIN_TELEGRAM_IDS") or "").split(",") if s.strip()}
BOT_LOCK_ID = int(os.getenv("BOT_LOCK_ID", "911001"))
ALERTS_LOCK_ID = int(os.getenv("ALERTS_LOCK_ID", "911002"))

_BOT_HEART_INTERVAL = int(os.getenv("BOT_HEART_INTERVAL_SECONDS", "60"))
_BOT_HEART_TTL = int(os.getenv("BOT_HEART_TTL_SECONDS", "180"))

TRIAL_LIMIT_PER_COMMAND = int(os.getenv("TRIAL_LIMIT_PER_COMMAND", str(DEFAULT_FREE_LIMIT)))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

# ───────────────── Binance symbols cache ─────────────────
_BINANCE_SYMBOLS: dict[str, str] = {}
_BINANCE_LAST_FETCH = 0.0
_BINANCE_TTL = int(os.getenv("BINANCE_EXCHANGEINFO_TTL", "3600"))

def _refresh_binance_symbols(force: bool = False):
    global _BINANCE_LAST_FETCH, _BINANCE_SYMBOLS
    now = time.time()
    if (not force) and (now - _BINANCE_LAST_FETCH < _BINANCE_TTL) and _BINANCE_SYMBOLS:
        return
    try:
        r = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=15)
        data = r.json()
        mapping = {}
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            base = s.get("baseAsset", "")
            quote = s.get("quoteAsset", "")
            symbol = s.get("symbol", "")
            if quote == "USDT" and base and symbol:
                mapping[base.upper()] = symbol.upper()
        if mapping:
            _BINANCE_SYMBOLS = mapping
            _BINANCE_LAST_FETCH = now
            print({"msg": "binance_symbols_loaded", "count": len(mapping)})
    except Exception as e:
        print({"msg": "binance_symbols_error", "error": str(e)})

def resolve_symbol_auto(symbol: str | None) -> str | None:
    if not symbol:
        return None
    symbol = symbol.upper().strip()
    pair = resolve_symbol(symbol)
    if pair:
        return pair
    _refresh_binance_symbols()
    pair = _BINANCE_SYMBOLS.get(symbol)
    if pair:
        return pair
    _refresh_binance_symbols(force=True)
    return _BINANCE_SYMBOLS.get(symbol)

# ──────────────── Helpers / UI ────────────────
def target_msg(update: Update):
    return update.message or (update.callback_query.message if update.callback_query else None)

def paypal_upgrade_url_for(tg_id: str | None) -> str | None:
    if WEB_URL and (PAYPAL_PLAN_ID or PAYPAL_SUBSCRIBE_URL) and tg_id:
        plan = PAYPAL_PLAN_ID or ""
        return f"{WEB_URL}/billing/paypal/start?tg={tg_id}" + (f"&plan_id={plan}" if plan else "")
    return PAYPAL_SUBSCRIBE_URL

def main_menu_keyboard(tg_id: str | None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Price BTC", callback_data="go:price:BTC"),
         InlineKeyboardButton("🔔 My Alerts", callback_data="go:myalerts")],
        [InlineKeyboardButton("⏱️ Set Alert Help", callback_data="go:setalerthelp"),
         InlineKeyboardButton("ℹ️ Help", callback_data="go:help")],
        [InlineKeyboardButton("🆘 Support", callback_data="go:support")]
    ]
    u = paypal_upgrade_url_for(tg_id)
    if u: rows.append([InlineKeyboardButton("💎 Upgrade with PayPal", url=u)])
    return InlineKeyboardMarkup(rows)

def upgrade_keyboard(tg_id: str | None):
    u = paypal_upgrade_url_for(tg_id)
    return InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade with PayPal", url=u)]]) if u else None

def start_text() -> str:
    return (
        "<b>Crypto Alerts Bot</b>\n"
        "⚡ Fast prices • 🧪 Diagnostics • 🔔 Alerts\n\n"
        "<b>What you can do — quick</b>\n"
        "• Prices: <code>/price BTC</code>, mini <code>/chart BTC</code>\n"
        "• Alerts: <code>/setalert BTC &gt; 110000</code>, <code>/myalerts</code> (delete buttons)\n"
        "• Market tools: <code>/feargreed</code>, <code>/funding</code>, <code>/topgainers</code>, <code>/toplosers</code>, <code>/news</code>, <code>/dca</code>\n"
        "• Plus pack: <code>/dailyai</code>, <code>/advisor</code>, <code>/whatif</code>, <code>/portfolio_sim</code>, <code>/impactnews</code>, <code>/topalertsboard</code>\n"
        "• Advisor (persistent): <code>/setadvisor</code>, <code>/myadvisor</code>, <code>/rebalance_now</code>\n"
        "• Alerts feedback: automatic follow-up +2h/+6h\n"
        "• Alts & Presales: <code>/listalts</code>, <code>/listpresales</code>, <code>/alts &lt;SYMBOL&gt;</code>\n\n"
        "💎 <b>Premium</b>: unlimited usage on all commands\n"
        f"🆓 <b>Free trial</b>: up to <b>{TRIAL_LIMIT_PER_COMMAND}</b> uses per command.\n"
    )

def safe_chunks(s: str, limit: int = 3800):
    while s:
        yield s[:limit]
        s = s[limit:]

def op_from_rule(rule: str) -> str:
    return ">" if rule == "price_above" else "<"

# ─────────────── FastAPI health ───────────────
health_app = FastAPI()
_BOT_HEART_BEAT_AT = None
_BOT_HEART_STATUS = "unknown"
_ALERTS_LAST_OK_AT = None
_ALERTS_LAST_RESULT = None

@health_app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"ok": True, "service": "crypto-alerts-server"}

@health_app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}

@health_app.api_route("/botok", methods=["GET", "HEAD"])
def botok():
    now = datetime.utcnow()
    stale = (_BOT_HEART_BEAT_AT is None) or ((now - _BOT_HEART_BEAT_AT) > timedelta(seconds=_BOT_HEART_TTL))
    return {
        "bot": ("stale" if stale else _BOT_HEART_STATUS),
        "last": (_BOT_HEART_BEAT_AT.isoformat() + "Z") if _BOT_HEART_BEAT_AT else None,
        "ttl_seconds": _BOT_HEART_TTL,
        "interval_seconds": _BOT_HEART_INTERVAL,
    }

@health_app.api_route("/alertsok", methods=["GET", "HEAD"])
def alertsok():
    return {
        "last_ok": (_ALERTS_LAST_OK_AT.isoformat() + "Z") if _ALERTS_LAST_OK_AT else None,
        "last_result": _ALERTS_LAST_RESULT or {},
        "expected_interval_seconds": INTERVAL_SECONDS,
    }

@health_app.get("/billing/paypal/start")
def paypal_start(tg: str | None = Query(None), plan_id: str | None = Query(None)):
    plan = (plan_id or PAYPAL_PLAN_ID or "").strip()
    target = PAYPAL_SUBSCRIBE_URL or (f"https://www.paypal.com/webapps/billing/plans/subscribe?plan_id={plan}" if plan else None)
    if not target:
        return JSONResponse({"error": "No PAYPAL_SUBSCRIBE_URL and no plan_id"}, status_code=400)
    try:
        parsed = urlparse(target)
        q = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if tg and "tg" not in q:
            q["tg"] = tg
        if plan and "plan_id" not in q:
            q["plan_id"] = plan
        new_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(q), parsed.fragment))
        return RedirectResponse(new_url, status_code=302)
    except Exception as e:
        return PlainTextResponse(f"Redirect error: {e}", status_code=500)

def bot_heartbeat_loop():
    global _BOT_HEART_BEAT_AT, _BOT_HEART_STATUS
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    print({"msg": "bot_heartbeat_started", "interval": _BOT_HEART_INTERVAL})
    while True:
        try:
            r = requests.get(url, timeout=10)
            _BOT_HEART_STATUS = "ok" if (r.status_code == 200 and r.json().get("ok") is True) else "fail"
        except Exception:
            _BOT_HEART_STATUS = "fail"
        _BOT_HEART_BEAT_AT = datetime.utcnow()
        time.sleep(_BOT_HEART_INTERVAL)

# ─────────────── Trial guard ───────────────
from usage_limits import increment_and_check
async def pre_command_usage_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        text_msg = (msg.text or msg.caption or "").strip()
        if not text_msg.startswith("/"): return
        raw = text_msg.split()[0]
        name = raw[1:]
        if "@" in name: name = name.split("@", 1)[0]
        cmd = name.lower()

        tg_id = str(update.effective_user.id)
        plan = build_plan_info(tg_id, _ADMIN_IDS)
        if plan.is_admin: return

        res = increment_and_check(plan.user_id, cmd, is_premium=plan.is_premium, limit=TRIAL_LIMIT_PER_COMMAND)
        if res.allowed: return

        upsell = (
            f"🚫 You’ve used /{cmd} {res.limit} times on the Free trial.\n"
            f"💎 Upgrade to Premium for unlimited usage on all commands."
        )
        kb = upgrade_keyboard(tg_id)
        await msg.reply_text(upsell, reply_markup=kb)
        raise ApplicationHandlerStop()
    except ApplicationHandlerStop:
        raise
    except Exception as e:
        print({"msg": "usage_guard_error", "error": str(e)})

# ─────────────── Commands ───────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    _ = build_plan_info(tg_id, _ADMIN_IDS)
    await target_msg(update).reply_text(
        start_text(),
        reply_markup=main_menu_keyboard(tg_id),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    help_html = (
        "<b>Help</b>\n\n"
        f"Free trial: up to {TRIAL_LIMIT_PER_COMMAND} uses per command. Premium: unlimited.\n\n"
        "• <code>/price &lt;SYMBOL&gt;</code> → Spot price (auto-detect new Binance USDT listings)\n"
        "• <code>/setalert &lt;SYMBOL&gt; &lt;op&gt; &lt;value&gt;</code>  e.g. <code>/setalert BTC &gt; 110000</code>\n"
        "• <code>/myalerts</code> → list your alerts\n"
        "• <code>/delalert &lt;id&gt;</code>, <code>/clearalerts</code> → Premium\n"
        "• <code>/whoami</code> → plan info  •  <code>/cancel_autorenew</code>\n"
        "• <code>/support &lt;message&gt;</code> → contact admins\n\n"
        "<b>Market Tools</b>\n"
        "• <code>/feargreed</code> • <code>/funding [SYMBOL]</code> • <code>/topgainers</code> • <code>/toplosers</code>\n"
        "• <code>/chart &lt;SYMBOL&gt;</code> • <code>/news [N]</code> • <code>/dca &lt;amount&gt; &lt;buys&gt; &lt;symbol&gt;</code>\n"
        "• <code>/pumplive on|off [threshold%]</code>\n\n"
        "<b>Alts / Presales</b>\n"
        "• <code>/alts &lt;SYMBOL&gt;</code> • <code>/listalts</code> • <code>/listpresales</code>\n\n"
        "<b>Plus Pack</b>\n"
        "• <code>/dailyai [SYMBOLS]</code> • <code>/advisor &lt;budget&gt; &lt;low|medium|high&gt;</code>\n"
        "• <code>/whatif &lt;SYMBOL&gt; &lt;long|short&gt; &lt;entry&gt; [leverage]</code>\n"
        "• <code>/portfolio_sim &lt;positions&gt; &lt;shock&gt;</code>\n"
        "• <code>/impactnews &lt;headline&gt;</code> • <code>/topalertsboard</code>\n"
        "• <code>/setadvisor</code> • <code>/myadvisor</code> • <code>/rebalance_now</code>\n"
    )
    for chunk in safe_chunks(help_html):
        await target_msg(update).reply_text(
            chunk,
            reply_markup=upgrade_keyboard(tg_id),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan = build_plan_info(str(update.effective_user.id), _ADMIN_IDS)
    await target_msg(update).reply_text(
        f"You are: {'admin' if plan.is_admin else 'user'}\nPremium: {plan.is_premium}\n{plan_status_line(plan)}"
    )

async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = (context.args[0] if context.args else "BTC").upper()
    pair = resolve_symbol_auto(symbol)
    if pair:
        price = fetch_price_binance(pair)
        if price is None:
            await target_msg(update).reply_text("Price fetch failed. Try again later.")
            return
        await target_msg(update).reply_text(f"{pair}: {price:.6f} USDT")
        return
    info = get_off_binance_info(symbol)
    if info:
        lines = [f"ℹ️ <b>{html.escape(info.get('name', symbol))}</b>\n{html.escape(info.get('note',''))}".strip()]
        for title, url in info.get("links", []):
            lines.append(f"• <a href=\"{html.escape(url)}\">{html.escape(title)}</a>")
        await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    await target_msg(update).reply_text(
        "Unknown symbol. Try BTC, ETH, SOL… or <code>/alts SYMBOL</code>.",
        parse_mode=ParseMode.HTML,
    )

ALERT_RE = re.compile(r"^(?P<sym>[A-Za-z0-9/]+)\s*(?P<op>>|<)\s*(?P<val>[0-9]+(\.[0-9]+)?)$")

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await target_msg(update).reply_text(
            "Usage: /setalert <SYMBOL> <op> <value>\nExample: /setalert BTC > 110000"
        )
        return
    m = ALERT_RE.match(" ".join(context.args))
    if not m:
        await target_msg(update).reply_text("Format error. Example: /setalert BTC > 110000")
        return
    sym, op, val = m.group("sym"), m.group("op"), float(m.group("val"))
    pair = resolve_symbol_auto(sym)
    if not pair:
        await target_msg(update).reply_text(
            "Unknown symbol. Try BTC, ETH, SOL … or <code>/alts SYMBOL</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    allowed, denial, remaining = can_create_alert(plan)
    if not allowed:
        await target_msg(update).reply_text(denial)
        return
    rule = "price_above" if op == ">" else "price_below"
    try:
        with session_scope() as session:
            row = session.execute(
                text(
                    """
                INSERT INTO alerts (user_id, symbol, rule, value, cooldown_seconds, user_seq, enabled)
                VALUES (:uid, :sym, :rule, :val, :cooldown,
                        (SELECT COALESCE(MAX(user_seq),0)+1 FROM alerts WHERE user_id=:uid),
                        TRUE)
                RETURNING id, user_seq
                """
                ),
                {"uid": plan.user_id, "sym": pair, "rule": rule, "val": val, "cooldown": 900},
            ).first()
            user_seq = row.user_seq
        await target_msg(update).reply_text(f"✅ Alert A{user_seq} set: {pair} {op} {val}")
    except Exception as e:
        await target_msg(update).reply_text(f"❌ Could not create alert: {e}")

def _alert_buttons(aid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete", callback_data=f"del:{aid}")]])

async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)
    with session_scope() as session:
        rows = session.execute(
            text(
                "SELECT id, user_seq, symbol, rule, value, enabled FROM alerts "
                "WHERE user_id=:uid ORDER BY id DESC LIMIT 20"
            ),
            {"uid": plan.user_id},
        ).all()
    if not rows:
        await target_msg(update).reply_text(f"No alerts in DB.\n{plan_status_line(plan)}")
        return
    for r in rows:
        op = op_from_rule(r.rule)
        await target_msg(update).reply_text(
            f"A{r.user_seq}  {r.symbol} {op} {r.value}  {'ON' if r.enabled else 'OFF'}",
            reply_markup=_alert_buttons(r.id),
        )

async def cmd_delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan = build_plan_info(str(update.effective_user.id), _ADMIN_IDS)
    if not plan.has_unlimited:
        await target_msg(update).reply_text("This feature is for Premium users. Upgrade to delete alerts.")
        return
    if not context.args:
        await target_msg(update).reply_text("Usage: /delalert <id>")
        return
    try:
        aid = int(context.args[0])
    except Exception:
        await target_msg(update).reply_text("Bad id")
        return
    with session_scope() as session:
        res = session.execute(
            text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
            {"id": aid, "uid": plan.user_id},
        )
        session.commit()
    await target_msg(update).reply_text("Deleted." if (res.rowcount or 0) > 0 else "Nothing deleted (check id/ownership).")

async def cmd_clearalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan = build_plan_info(str(update.effective_user.id), _ADMIN_IDS)
    if not plan.has_unlimited:
        await target_msg(update).reply_text("This feature is for Premium users. Upgrade to clear alerts.")
        return
    with session_scope() as session:
        session.execute(text("DELETE FROM alerts WHERE user_id=:uid"), {"uid": plan.user_id})
        session.commit()
    await target_msg(update).reply_text("All your alerts were deleted.")

# ─────────────── Alts / Presales (HTML-safe) ───────────────
async def cmd_alts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await target_msg(update).reply_text("Usage: /alts <SYMBOL>")
            return
        sym = (context.args[0] or "").upper().strip()
        info = get_off_binance_info(sym)
        if not info:
            await target_msg(update).reply_text("No curated info for that symbol.")
            return
        lines = [f"ℹ️ <b>{html.escape(info.get('name', sym))}</b>\n{html.escape(info.get('note',''))}".strip()]
        for title, url in info.get("links", []):
            lines.append(f"• <a href=\"{html.escape(url)}\">{html.escape(title)}</a>")
        await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        await target_msg(update).reply_text(f"Error: {e}")

async def cmd_listalts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        syms = list_off_binance()
        if not syms:
            await target_msg(update).reply_text("No curated tokens configured yet.")
            return
        lines = ["🌱 <b>Curated Off-Binance & Community</b>"]
        lines += [f"• <code>{html.escape(s)}</code>" for s in syms]
        lines.append("\nTip: /alts &lt;SYMBOL&gt; for notes & links.")
        await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        await target_msg(update).reply_text(f"Error: {e}")

async def cmd_listpresales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        syms = list_presales()
        if not syms:
            await target_msg(update).reply_text("No presales listed yet.")
            return
        lines = ["🟠 <b>Curated Presales</b>"]
        lines += [f"• <code>{html.escape(s)}</code>" for s in syms]
        lines.append("\nTip: /alts &lt;SYMBOL&gt; for notes & links. DYOR • High risk.")
        await target_msg(update).reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        await target_msg(update).reply_text(f"Error: {e}")

# ─────────────── Callbacks, worker, run (unchanged) ───────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Loading...", show_alert=False)
    data = (query.data or "").strip()
    tg_id = str(query.from_user.id)
    plan = build_plan_info(tg_id, _ADMIN_IDS)

    if data == "go:help":
        await cmd_help(update, context); return
    if data == "go:myalerts":
        await cmd_myalerts(update, context); return
    if data.startswith("go:price:"):
        sym = data.split(":", 2)[2]
        pair = resolve_symbol_auto(sym)
        price = fetch_price_binance(pair) if pair else None
        await query.message.reply_text("Price fetch failed." if price is None else f"{pair}: {price:.6f} USDT")
        return
    if data == "go:setalerthelp":
        await query.message.reply_text("Examples:\n• /setalert BTC > 110000\n• /setalert ETH < 2000")
        return
    if data == "go:support":
        await query.message.reply_text("Send /support <message>", reply_markup=upgrade_keyboard(tg_id))
        return

    if data.startswith("del:"):
        try:
            aid = int(data.split(":", 1)[1])
        except Exception:
            await query.edit_message_text("Bad id."); return
        with session_scope() as s:
            owner = s.execute(text("SELECT user_id FROM alerts WHERE id=:id"), {"id": aid}).first()
            if not owner:
                await query.edit_message_text("Alert not found."); return
            if owner.user_id != plan.user_id:
                await query.edit_message_text("You can delete only your own alerts."); return
            s.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
                      {"id": aid, "uid": plan.user_id})
            s.commit()
        await query.edit_message_text("✅ Deleted alert.")
        return

    if data.startswith("ack:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("Bad callback."); return
        action, aid_str = parts[1], parts[2]
        try:
            aid = int(aid_str)
        except Exception:
            await query.answer("Bad id."); return
        if action == "keep":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.answer("Kept 👍")
            except Exception:
                await query.answer("Kept.")
            return
        if action == "del":
            with session_scope() as s:
                owner = s.execute(text("SELECT user_id FROM alerts WHERE id=:id"), {"id": aid}).first()
                if not owner:
                    await query.edit_message_text("Alert not found."); return
                plan = build_plan_info(str(query.from_user.id), _ADMIN_IDS)
                if owner.user_id != plan.user_id:
                    await query.edit_message_text("You can delete only your own alerts."); return
                s.execute(text("DELETE FROM alerts WHERE id=:id AND user_id=:uid"),
                          {"id": aid, "uid": plan.user_id})
                s.commit()
            try:
                await query.edit_message_text("✅ Alert deleted.")
            except Exception:
                await query.answer("Deleted.")
            return

def alerts_loop():
    global _ALERTS_LAST_OK_AT, _ALERTS_LAST_RESULT
    if not RUN_ALERTS:
        print({"msg": "alerts_disabled_env"}); return
    lock_conn = engine.connect()
    got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": ALERTS_LOCK_ID}).scalar()
    if not got:
        print({"msg": "alerts_lock_skipped"}); lock_conn.close(); return
    print({"msg": "alerts_loop_start", "interval": INTERVAL_SECONDS}); init_db()
    try:
        while True:
            ts = datetime.utcnow().isoformat()
            try:
                with session_scope() as s:
                    counters = run_alert_cycle(s)
                _ALERTS_LAST_RESULT = {"ts": ts, **counters}
                _ALERTS_LAST_OK_AT = datetime.utcnow()
                print({"msg": "alert_cycle", **_ALERTS_LAST_RESULT})
            except Exception as e:
                print({"msg": "alert_cycle_error", "ts": ts, "error": str(e)})
            time.sleep(INTERVAL_SECONDS)
    finally:
        try:
            lock_conn.close()
        except Exception:
            pass

def delete_webhook_if_any():
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
        print({"msg": "delete_webhook", "status": r.status_code, "body": r.text[:160]})
    except Exception as e:
        print({"msg": "delete_webhook_exception", "error": str(e)})

def run_bot():
    if not RUN_BOT:
        print({"msg": "bot_disabled_env"}); return

    lock_conn = engine.connect()
    got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": BOT_LOCK_ID}).scalar()
    if not got:
        print({"msg": "bot_lock_skipped"}); lock_conn.close(); return
    try:
        try:
            delete_webhook_if_any()
        except Exception:
            pass

        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .read_timeout(40)
            .connect_timeout(15)
            .build()
        )

        app.add_handler(MessageHandler(filters.COMMAND, pre_command_usage_guard), group=-1)

        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("whoami", cmd_whoami))
        app.add_handler(CommandHandler("price", cmd_price))
        app.add_handler(CommandHandler("alts", cmd_alts))
        app.add_handler(CommandHandler("listalts", cmd_listalts))
        app.add_handler(CommandHandler("listpresales", cmd_listpresales))
        app.add_handler(CommandHandler("setalert", cmd_setalert))
        app.add_handler(CommandHandler("myalerts", cmd_myalerts))
        app.add_handler(CommandHandler("delalert", cmd_delalert))
        app.add_handler(CommandHandler("clearalerts", cmd_clearalerts))

        register_extra_handlers(app)
        register_plus_handlers(app)
        register_advisor_handlers(app)
        register_admin_handlers(app, _ADMIN_IDS)

        app.add_handler(CallbackQueryHandler(on_callback))

        print({"msg": "bot_start"})
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    finally:
        try:
            lock_conn.close()
        except Exception:
            pass

def main():
    init_db()
    init_extras()
    port = int(os.getenv("PORT", "10000"))
    threading.Thread(
        target=lambda: uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="info"),
        daemon=True
    ).start()
    threading.Thread(target=bot_heartbeat_loop, daemon=True).start()
    start_advisor_scheduler()
    start_feedback_scheduler()
    threading.Thread(target=alerts_loop, daemon=True).start()
    run_bot()

if __name__ == "__main__":
    main()
