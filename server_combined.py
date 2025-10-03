# server_combined.py
# FastAPI + Telegram Bot (polling) σε ένα αρχείο, με 10ήμερο trial,
# admin tools και συμβατότητα με παλιές/νέες worker_logic υπογραφές.

import os
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable, getfullargspec

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Conflict as TgConflict, TimedOut as TgTimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
RUN_BOT = os.getenv("RUN_BOT", "true").lower() not in ("0", "false", "no")
FREE_ALERT_LIMIT = int(os.getenv("FREE_ALERT_LIMIT", "10"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "10"))
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "5254014824")
_ADMIN_IDS = {s.strip() for s in ADMIN_IDS_RAW.split(",") if s.strip()}
DATABASE_URL = os.getenv("DATABASE_URL", "")
BOT_LOCK_ID = int(os.getenv("BOT_LOCK_ID", "921001"))
ALERTS_LOCK_ID = int(os.getenv("ALERTS_LOCK_ID", "911002"))
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    print({"msg": "missing_env", "var": "BOT_TOKEN"})

# ---------- DB ----------
engine: Optional[Engine] = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
    except Exception as e:
        print({"msg": "db_engine_error", "error": str(e)})
        engine = None
else:
    print({"msg": "missing_env", "var": "DATABASE_URL"})

# ---------- Optional modules ----------
def _opt_import(name: str):
    try:
        return __import__(name)
    except Exception:
        return None

worker_logic = _opt_import("worker_logic")
commands_extra = _opt_import("commands_extra")
admin_commands = _opt_import("admin_commands")

# ---------- Web app ----------
app = FastAPI()
# Alias για παλιό Render start command που ζητά "health_app"
health_app = app

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"

@app.head("/", response_class=PlainTextResponse)
async def root_head():
    return PlainTextResponse(content="", status_code=200)

@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "OK"

@app.head("/health", response_class=PlainTextResponse)
async def health_head():
    return PlainTextResponse(content="", status_code=200)

@app.get("/botok", response_class=PlainTextResponse)
async def botok():
    return "OK"

@app.head("/botok", response_class=PlainTextResponse)
async def botok_head():
    return PlainTextResponse(content="", status_code=200)

@app.get("/alertsok", response_class=PlainTextResponse)
async def alertsok():
    return "OK"

@app.head("/alertsok", response_class=PlainTextResponse)
async def alertsok_head():
    return PlainTextResponse(content="", status_code=200)

# ---------- Utils ----------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def delete_webhook_if_any():
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook"
    try:
        r = httpx.get(url, timeout=10)
        print({"msg": "delete_webhook", "status": r.status_code, "body": r.text[:120]})
    except Exception as e:
        print({"msg": "delete_webhook_error", "error": str(e)})

def safe_execute(sql: str, params: dict = None, fetch: bool = False):
    if engine is None:
        return None
    try:
        with engine.begin() as conn:
            res = conn.execute(text(sql), params or {})
            if fetch:
                return [dict(row._mapping) for row in res]
            return None
    except Exception as e:
        print({"msg": "sql_error", "error": str(e), "sql": sql})
        return None

def ensure_user_on_start(user_id: int, tg_username: Optional[str]) -> None:
    if engine is None:
        return
    try:
        safe_execute(
            """
            INSERT INTO users (telegram_id, is_premium, trial_started_at, trial_expires_at, username)
            VALUES (:tid, FALSE, NOW(), NOW() + INTERVAL :days, :uname)
            ON CONFLICT (telegram_id) DO UPDATE
              SET username = COALESCE(EXCLUDED.username, users.username),
                  trial_started_at = COALESCE(users.trial_started_at, NOW()),
                  trial_expires_at = COALESCE(users.trial_expires_at, NOW() + INTERVAL :days)
            """,
            {"tid": user_id, "uname": tg_username, "days": f"'{TRIAL_DAYS} days'"},
        )
    except Exception as e:
        print({"msg": "ensure_user_error", "error": str(e)})

def get_user_access(user_id: int) -> Dict[str, Any]:
    is_admin = str(user_id) in _ADMIN_IDS
    access = {
        "is_admin": is_admin,
        "is_premium": is_admin,
        "in_trial": True,
        "trial_expires_at": None,
    }
    if engine is None:
        return access
    try:
        rows = safe_execute(
            """
            SELECT telegram_id, is_premium, trial_started_at, trial_expires_at
            FROM users
            WHERE telegram_id = :tid
            """,
            {"tid": user_id},
            fetch=True,
        )
        if not rows:
            return access
        row = rows[0]
        premium = bool(row.get("is_premium", False))
        expires = row.get("trial_expires_at")
        in_trial = True
        if isinstance(expires, datetime):
            in_trial = expires >= utcnow()
        access["is_premium"] = premium or is_admin
        access["in_trial"] = in_trial or is_admin
        access["trial_expires_at"] = expires
        return access
    except Exception as e:
        print({"msg": "get_user_access_error", "error": str(e)})
        return access

async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None:
        return
    uid = update.effective_user.id
    if str(uid) in _ADMIN_IDS:
        return
    acc = get_user_access(uid)
    if acc["is_premium"] or acc["in_trial"]:
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Message Admin", url="https://t.me/CryptoAlerts77")]])
    msg = "⚠️ Η δοκιμαστική περίοδος έληξε.\nΣτείλε μήνυμα στον Admin για να ενεργοποιηθεί πρόσβαση."
    try:
        await update.effective_chat.send_message(msg, reply_markup=kb)
    except Exception:
        pass
    raise asyncio.CancelledError

# ---------- Commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        ensure_user_on_start(update.effective_user.id, update.effective_user.username)
    lines = [
        "👋 Καλώς ήρθες στο *Crypto Alerts 77*!",
        f"🎁 Δωρεάν δοκιμή: *{TRIAL_DAYS} ημέρες* με πλήρη πρόσβαση σε όλες τις εντολές.",
        "",
        "📋 *Κύριες εντολές*: ",
        "• /help – Δες όλες τις δυνατότητες",
        "• /price BTC – Τρέχουσα τιμή",
        "• /setalert BTC > 70000 – Alert τιμής",
        "• /myalerts – Τα alerts σου",
        "• /delalert <id> – Σβήσε alert",
        "• /clearalerts – Σβήσε όλα",
        "",
        "🪙 *Alts & Presales*: /alts, /listalts, /listpresales",
        "",
        "ℹ️ /whoami – Δείξε status λογαριασμού",
        "🆘 /support – Επικοινωνία με admin",
    ]
    acc = get_user_access(update.effective_user.id) if update.effective_user else {}
    exp_str = ""
    if acc.get("trial_expires_at"):
        try:
            g = acc["trial_expires_at"].astimezone(timezone.utc)
            exp_str = f"\n⏳ Λήξη trial: *{g:%Y-%m-%d %H:%M UTC}*"
        except Exception:
            pass
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 Βοήθεια / Help", callback_data="help_open")],
        [InlineKeyboardButton("💬 Επικοινωνία Admin", url="https://t.me/CryptoAlerts77")],
    ])
    await update.message.reply_text("\n".join(lines) + exp_str, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 *Βοήθεια*\n\n"
        "Τιμές & Alerts:\n"
        "• /price <SYMBOL> – τρέχουσα τιμή (π.χ. /price BTC)\n"
        "• /setalert <SYMBOL> [>/<] <τιμή> – (π.χ. /setalert BTC > 70000)\n"
        "• /myalerts – λίστα alerts\n"
        "• /delalert <id> – διαγραφή alert\n"
        "• /clearalerts – καθαρισμός όλων\n\n"
        "Alts & Presales:\n"
        "• /alts <SYMBOL> – σημείωμα/links\n"
        "• /listalts – curated off-binance\n"
        "• /listpresales – presales\n\n"
        "Λογαριασμός & Support:\n"
        "• /whoami – στοιχεία & trial\n"
        "• /support – επαφή με admin\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    acc = get_user_access(update.effective_user.id)
    lines = [
        f"👤 User ID: `{update.effective_user.id}`",
        f"👑 Admin: {'✅' if acc.get('is_admin') else '❌'}",
        f"💎 Premium: {'✅' if acc.get('is_premium') else '❌'}",
        f"🧪 Trial ενεργό: {'✅' if acc.get('in_trial') else '❌'}",
    ]
    exp = acc.get("trial_expires_at")
    if exp:
        try:
            g = exp.astimezone(timezone.utc)
            lines.append(f"⏳ Trial λήγει: *{g:%Y-%m-%d %H:%M UTC}*")
        except Exception:
            pass
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Message Admin", url="https://t.me/CryptoAlerts77")]])
    await update.message.reply_text("Χρειάζεσαι βοήθεια; Μίλα με τον Admin 👇", reply_markup=kb)

# --- Stubs / core commands (κρατάμε συμβατότητα) ---
async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = None
    if context.args:
        symbol = (context.args[0] or "").upper().strip()
    price_text = "Use: /price BTC"
    try:
        if symbol:
            p = None
            if worker_logic and hasattr(worker_logic, "fetch_price_binance"):
                try:
                    p = worker_logic.fetch_price_binance(symbol)
                except Exception as e:
                    print({"msg": "price_binance_error", "error": str(e)})
            price_text = f"{symbol}: {p}" if p is not None else f"{symbol}: price temporarily unavailable"
    except Exception as e:
        print({"msg": "cmd_price_error", "error": str(e)})
    await update.message.reply_text(price_text)

async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("OK, send: /setalert BTC > 70000 (stub).")

async def cmd_myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Your alerts (stub).")

async def cmd_delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Deleted (stub).")

async def cmd_clearalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("All alerts cleared (stub).")

# --- Alts / Presales ---
async def cmd_alts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Alts note (stub).")

async def cmd_listalts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Curated off-binance tokens (stub).")

async def cmd_listpresales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Presales list (stub).")

# --- Admin only ---
def _is_admin(user_id: int) -> bool:
    return str(user_id) in _ADMIN_IDS

async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    cnt = 0
    if engine:
        rows = safe_execute("SELECT COUNT(*) AS c FROM users", fetch=True)
        if rows and "c" in rows[0]:
            cnt = rows[0]["c"]
    await update.message.reply_text(f"Users with row in DB: {cnt}")

async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    try:
        tid = int(context.args[0]); days = int(context.args[1])
    except Exception:
        await update.message.reply_text("Use: /extend <telegram_id> <days>")
        return
    if engine:
        safe_execute(
            "UPDATE users SET trial_expires_at = COALESCE(trial_expires_at, NOW()) + INTERVAL :days WHERE telegram_id = :tid",
            {"tid": tid, "days": f"'{days} days'"},
        )
    await update.message.reply_text(f"Extended {tid} by {days} days")

async def cmd_setpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    try:
        tid = int(context.args[0])
        flag = (context.args[1] or "").lower() in ("on", "true", "1", "yes")
    except Exception:
        await update.message.reply_text("Use: /setpremium <telegram_id> <on|off>")
        return
    if engine:
        safe_execute("UPDATE users SET is_premium = :f WHERE telegram_id = :tid", {"tid": tid, "f": flag})
    await update.message.reply_text(f"Premium for {tid}: {flag}")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    lines = ["Stats:"]
    if engine:
        r1 = safe_execute("SELECT COUNT(*) AS c FROM users", fetch=True)
        if r1: lines.append(f"Total users: {r1[0].get('c')}")
        r2 = safe_execute("SELECT COUNT(*) AS c FROM users WHERE is_premium = TRUE", fetch=True)
        if r2: lines.append(f"Premium users: {r2[0].get('c')}")
    await update.message.reply_text("\n".join(lines))

# --- Optional external registrations ---
def register_extra_handlers(app_obj: Application):
    if commands_extra and hasattr(commands_extra, "register_extra_handlers"):
        try:
            commands_extra.register_extra_handlers(app_obj)
        except Exception as e:
            print({"msg": "register_extra_handlers_error", "error": str(e)})

def register_admin_handlers(app_obj: Application, admin_ids: set):
    if admin_commands and hasattr(admin_commands, "register_admin_handlers"):
        try:
            admin_commands.register_admin_handlers(app_obj, admin_ids)
        except Exception as e:
            print({"msg": "register_admin_handlers_error", "error": str(e)})

# ---------- Bot tasks ----------
_BOT_TASK: Optional[asyncio.Task] = None
_ALERTS_TASK: Optional[asyncio.Task] = None
_BOT_LOCK_HELD = False

async def bot_polling_task():
    global _BOT_LOCK_HELD
    if not RUN_BOT or not BOT_TOKEN:
        print({"msg": "bot_disabled_env"})
        return

    # advisory lock
    while True:
        try:
            if engine:
                with engine.connect() as c:
                    got = c.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": BOT_LOCK_ID}).scalar()
            else:
                got = True
            if got:
                _BOT_LOCK_HELD = True
                print({"msg": "advisory_lock_acquired", "lock": "bot", "id": BOT_LOCK_ID})
                break
            print({"msg": "advisory_lock_busy", "lock": "bot", "id": BOT_LOCK_ID})
        except Exception as e:
            print({"msg": "advisory_lock_error", "lock": "bot", "error": str(e)})
        await asyncio.sleep(15)

    try:
        delete_webhook_if_any()

        app_obj = (
            Application.builder()
            .token(BOT_TOKEN)
            .read_timeout(40)
            .connect_timeout(15)
            .build()
        )

        # guard
        app_obj.add_handler(MessageHandler(filters.COMMAND, access_guard), group=0)

        # core
        app_obj.add_handler(CommandHandler("start", cmd_start))
        app_obj.add_handler(CommandHandler("help", cmd_help))
        app_obj.add_handler(CommandHandler("whoami", cmd_whoami))
        app_obj.add_handler(CommandHandler("support", cmd_support))

        # legacy
        app_obj.add_handler(CommandHandler("price", cmd_price))
        app_obj.add_handler(CommandHandler("setalert", cmd_setalert))
        app_obj.add_handler(CommandHandler("myalerts", cmd_myalerts))
        app_obj.add_handler(CommandHandler("delalert", cmd_delalert))
        app_obj.add_handler(CommandHandler("clearalerts", cmd_clearalerts))

        # alts
        app_obj.add_handler(CommandHandler("alts", cmd_alts))
        app_obj.add_handler(CommandHandler("listalts", cmd_listalts))
        app_obj.add_handler(CommandHandler("listpresales", cmd_listpresales))

        # admin
        app_obj.add_handler(CommandHandler("listusers", cmd_listusers))
        app_obj.add_handler(CommandHandler("extend", cmd_extend))
        app_obj.add_handler(CommandHandler("setpremium", cmd_setpremium))
        app_obj.add_handler(CommandHandler("stats", cmd_stats))

        # external
        register_extra_handlers(app_obj)
        register_admin_handlers(app_obj, _ADMIN_IDS)

        print({"msg": "bot_starting", "RUN_BOT": RUN_BOT, "admin_ids": list(_ADMIN_IDS),
               "free_alert_limit": FREE_ALERT_LIMIT, "trial_days": TRIAL_DAYS})

        await app_obj.initialize()
        await app_obj.start()
        await app_obj.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            poll_interval=1.0,
            timeout=40,
        )

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await app_obj.updater.stop()
            await app_obj.stop()
            await app_obj.shutdown()

    except TgConflict as e:
        print({"msg": "bot_conflict_exit", "error": str(e)})
    except TgTimedOut as e:
        print({"msg": "bot_timeout_exit", "error": str(e)})
    except Exception as e:
        print({"msg": "bot_generic_exit", "error": str(e)})
    finally:
        try:
            if engine and _BOT_LOCK_HELD:
                with engine.connect() as c:
                    c.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": BOT_LOCK_ID})
        except Exception:
            pass
        _BOT_LOCK_HELD = False
        print({"msg": "bot_task_exit"})

def _call_run_alert_cycle():
    """
    Καλεί το worker_logic.run_alert_cycle με ή χωρίς session,
    ανάλογα με την υπογραφή της τρέχουσας έκδοσης.
    """
    if not worker_logic or not hasattr(worker_logic, "run_alert_cycle"):
        return False, None  # evaluated, error

    func: Callable = worker_logic.run_alert_cycle
    try:
        spec = getfullargspec(func)
        wants_session = len(spec.args or []) >= 1  # π.χ. def run_alert_cycle(session)
    except Exception:
        wants_session = False

    if wants_session:
        # Δημιουργούμε connection/session και το περνάμε
        if engine is None:
            return False, "no_engine"
        try:
            with engine.begin() as conn:
                func(conn)
            return True, None
        except Exception as e:
            return False, str(e)
    else:
        # Παλιά εκδοχή χωρίς όρισμα
        try:
            func()
            return True, None
        except Exception as e:
            return False, str(e)

async def alerts_loop_task():
    interval = 60
    print({"msg": "alerts_loop_start", "interval": interval})
    while True:
        lock_held = False
        try:
            if engine:
                with engine.connect() as c:
                    got = c.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": ALERTS_LOCK_ID}).scalar()
                lock_held = bool(got)
            else:
                lock_held = True

            evaluated = 0
            triggered = 0
            errors = 0

            ok, err = _call_run_alert_cycle()
            if ok:
                evaluated = 1
            else:
                errors = 1
                if err:
                    print({"msg": "alerts_cycle_error", "error": err})

            print({"msg": "alert_cycle", "ts": str(utcnow()), "evaluated": evaluated, "triggered": triggered, "errors": errors})
        except Exception as e:
            print({"msg": "alerts_loop_error", "error": str(e)})
        finally:
            try:
                if engine and lock_held:
                    with engine.connect() as c:
                        c.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": ALERTS_LOCK_ID})
            except Exception:
                pass
        await asyncio.sleep(interval)

# ---------- FastAPI lifecycle ----------
@app.on_event("startup")
async def on_startup():
    print({"msg": "worker_extra_threads_started"})
    print({"msg": "startup_threads_spawned"})
    loop = asyncio.get_running_loop()
    global _BOT_TASK, _ALERTS_TASK
    _ALERTS_TASK = loop.create_task(alerts_loop_task())
    _BOT_TASK = loop.create_task(bot_polling_task())

@app.on_event("shutdown")
async def on_shutdown():
    pass

# ---------- Uvicorn entry ----------
def main():
    import uvicorn
    uvicorn.run("server_combined:app", host="0.0.0.0", port=PORT, reload=False, log_level="info")

if __name__ == "__main__":
    main()
