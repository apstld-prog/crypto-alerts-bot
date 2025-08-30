#!/usr/bin/env python3
import logging, os, sqlite3, re, time, random
import requests
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("Missing or invalid BOT_TOKEN env var")

PAYPAL_SUBSCRIBE_PAGE = os.getenv("PAYPAL_SUBSCRIBE_PAGE", "https://crypto-alerts-bot-k8i7.onrender.com/subscribe.html")
DB_PATH = os.getenv("DB_PATH", "bot.db")  # local SQLite (χωρίς persistent disk)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"
logging.basicConfig(level=logging.INFO)

# ================== CACHE / RETRIES ==================
PRICE_CACHE = {}
CACHE_TTL = 60.0
STALE_TTL = 300.0
RETRY_MAX = 3
RETRY_SLEEP = 0.35
_LAST_CALLS = deque(maxlen=12)

def _throttle():
    now = time.time()
    if _LAST_CALLS and now - _LAST_CALLS[-1] < 0.35:
        time.sleep(0.35 - (now - _LAST_CALLS[-1]))
    _LAST_CALLS.append(time.time())

def _sleep_jitter(base): time.sleep(base + random.uniform(0, 0.15))

# ================== SYMBOL MAP & NORMALIZE ==================
SYMBOL_TO_ID = {
    "btc":"bitcoin","eth":"ethereum","sol":"solana","bnb":"binancecoin","xrp":"ripple",
    "ada":"cardano","doge":"dogecoin","matic":"polygon","trx":"tron","avax":"avalanche-2",
    "dot":"polkadot","ltc":"litecoin","atom":"cosmos","link":"chainlink","xlm":"stellar",
    "etc":"ethereum-classic","cro":"cronos","near":"near","xtz":"tezos","algo":"algorand",
    "icp":"internet-computer","hbar":"hedera","apt":"aptos","op":"optimism","arb":"arbitrum",
    "fil":"filecoin","egld":"elrond-erd-2","vet":"vechain","kas":"kaspa","ton":"the-open-network",
    "sei":"sei-network","tia":"celestia","inj":"injective","sui":"sui","mina":"mina-protocol",
    "grt":"the-graph","axs":"axie-infinity","sand":"the-sandbox","mana":"decentraland",
    "ape":"apecoin","ftm":"fantom","rose":"oasis-network","rune":"thorchain","qnt":"quant-network",
    "aave":"aave","uni":"uniswap","cake":"pancakeswap-token","gmt":"stepn","pepe":"pepe",
    "bonk":"bonk","shib":"shiba-inu",
    "usdt":"tether","usdc":"usd-coin","dai":"dai","tusd":"true-usd"
}

GREEK_TO_LATIN = str.maketrans({
    "Α":"A","Β":"B","Ε":"E","Ζ":"Z","Η":"H","Ι":"I","Κ":"K","Μ":"M","Ν":"N","Ο":"O","Ρ":"P","Τ":"T","Υ":"Y","Χ":"X",
    "α":"a","β":"b","ε":"e","ζ":"z","η":"h","ι":"i","κ":"k","μ":"m","ν":"n","ο":"o","ρ":"p","τ":"t","υ":"y","χ":"x",
})
def normalize_symbol(s: str) -> str:
    s = s.strip().translate(GREEK_TO_LATIN)
    s = re.sub(r"[^0-9A-Za-z\-]", "", s)
    return s.lower()

# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        premium_active INTEGER DEFAULT 0,
        premium_until TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        op TEXT NOT NULL,
        threshold REAL NOT NULL,
        active INTEGER DEFAULT 1,
        created_at REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS subscriptions(
        subscription_id TEXT PRIMARY KEY,
        user_id INTEGER,
        status TEXT,
        payer_id TEXT,
        plan_id TEXT,
        last_event REAL
    )""")
    conn.commit()
    return conn
CONN = db()

def ensure_user(user_id: int):
    CONN.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    CONN.commit()

def is_premium(user_id: int) -> bool:
    row = CONN.execute("SELECT premium_active FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row[0])

def set_premium(user_id: int, active: bool):
    ensure_user(user_id)
    CONN.execute("UPDATE users SET premium_active=? WHERE user_id=?", (1 if active else 0, user_id))
    CONN.commit()

def set_premium_until(user_id: int, until_ts: float):
    """Θέτει ημερομηνία λήξης premium (grace). Επίσης ενεργοποιεί premium τώρα."""
    ensure_user(user_id)
    CONN.execute("UPDATE users SET premium_until=? WHERE user_id=?", (str(int(until_ts)), user_id))
    CONN.execute("UPDATE users SET premium_active=1 WHERE user_id=?", (user_id,))
    CONN.commit()

def check_premium_expirations_now():
    """Απενεργοποιεί premium για χρήστες που πέρασε το premium_until."""
    now = int(time.time())
    rows = CONN.execute("SELECT user_id, premium_until FROM users WHERE premium_until IS NOT NULL").fetchall()
    changed = 0
    for uid, until in rows:
        try:
            if until and str(until).isdigit() and int(until) < now:
                CONN.execute("UPDATE users SET premium_active=0, premium_until=NULL WHERE user_id=?", (uid,))
                changed += 1
        except Exception:
            pass
    if changed:
        CONN.commit()

def set_subscription_record(sub_id, user_id, status, payer_id=None, plan_id=None):
    CONN.execute("""INSERT INTO subscriptions(subscription_id,user_id,status,payer_id,plan_id,last_event)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(subscription_id) DO UPDATE SET
                      user_id=COALESCE(excluded.user_id, subscriptions.user_id),
                      status=excluded.status,
                      payer_id=COALESCE(excluded.payer_id, subscriptions.payer_id),
                      plan_id=COALESCE(excluded.plan_id, subscriptions.plan_id),
                      last_event=excluded.last_event""",
                 (sub_id, user_id, status, payer_id, plan_id, time.time()))
    CONN.commit()

# ================== Providers ==================
def binance_price_for_symbol(symbol_or_id: str):
    sym_map = {
        "bitcoin":"BTC","ethereum":"ETH","solana":"SOL","ripple":"XRP","cardano":"ADA",
        "dogecoin":"DOGE","polygon":"MATIC","tron":"TRX","avalanche-2":"AVAX","polkadot":"DOT",
        "litecoin":"LTC","internet-computer":"ICP","chainlink":"LINK","cosmos":"ATOM","stellar":"XLM",
        "algorand":"ALGO","hedera":"HBAR","aptos":"APT","optimism":"OP","arbitrum":"ARB",
        "filecoin":"FIL","vechain":"VET","quant-network":"QNT","uniswap":"UNI","aave":"AAVE",
        "injective":"INJ","fantom":"FTM","oasis-network":"ROSE","mina-protocol":"MINA","sui":"SUI",
        "the-graph":"GRT","axie-infinity":"AXS","the-sandbox":"SAND","decentraland":"MANA",
        "apecoin":"APE","stepn":"GMT","the-open-network":"TON","kaspa":"KAS","elrond-erd-2":"EGLD",
    }
    sym = symbol_or_id.upper()
    if sym not in sym_map.values():
        sym = sym_map.get(symbol_or_id.lower(), sym)
    pair = sym + "USDT"
    hosts = ["https://api.binance.com","https://api1.binance.com","https://api2.binance.com","https://api3.binance.com"]
    for attempt in range(RETRY_MAX):
        _throttle()
        host = hosts[attempt % len(hosts)]
        try:
            r = requests.get(f"{host}/api/v3/ticker/price", params={"symbol": pair}, timeout=8)
            if r.status_code == 200:
                data = r.json(); price = data.get("price")
                if price is not None: return float(price)
        except Exception: pass
        _sleep_jitter(RETRY_SLEEP)
    return None

def cg_simple_price(ids_csv: str) -> dict:
    for _ in range(RETRY_MAX):
        _throttle()
        try:
            r = requests.get(COINGECKO_SIMPLE, params={"ids": ids_csv, "vs_currencies": "usd"}, timeout=8)
            if r.status_code == 200: return r.json() or {}
        except Exception: pass
        _sleep_jitter(RETRY_SLEEP)
    return {}

def coincap_price(cg_id: str):
    for _ in range(RETRY_MAX):
        _throttle()
        try:
            r = requests.get(f"https://api.coincap.io/v2/assets/{cg_id}", timeout=8)
            if r.status_code == 200:
                data = r.json(); price = data.get("data", {}).get("priceUsd")
                if price is not None: return float(price)
        except Exception: pass
        _sleep_jitter(RETRY_SLEEP)
    return None

def cryptocompare_price(symbol_or_id: str):
    sym = symbol_or_id.upper()
    for _ in range(RETRY_MAX):
        _throttle()
        try:
            r = requests.get("https://min-api.cryptocompare.com/data/price",
                             params={"fsym": sym, "tsyms": "USD"}, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if "USD" in data: return float(data["USD"])
        except Exception: pass
        _sleep_jitter(RETRY_SLEEP)
    return None

# ================== PRICE RESOLVER ==================
def resolve_price_usd(symbol: str):
    cg_id = SYMBOL_TO_ID.get(symbol.lower(), symbol.lower())
    now = time.time()
    cached = PRICE_CACHE.get(cg_id)
    if cached and now - cached[1] <= CACHE_TTL: return cached[0]
    p = binance_price_for_symbol(symbol)
    if p is not None: PRICE_CACHE[cg_id] = (p, now); return p
    data = cg_simple_price(cg_id)
    if cg_id in data and "usd" in data[cg_id]:
        p = float(data[cg_id]["usd"]); PRICE_CACHE[cg_id] = (p, now); return p
    p3 = coincap_price(cg_id)
    if p3 is not None: PRICE_CACHE[cg_id] = (p3, now); return p3
    p4 = cryptocompare_price(symbol)
    if p4 is not None: PRICE_CACHE[cg_id] = (p4, now); return p4
    if cached and now - cached[1] <= STALE_TTL: return cached[0]
    return None

# ================== UI TEXTS ==================
WELCOME_TEXT = (
    "🪙 **Crypto Alerts Bot**\n"
    "_Fast prices • Diagnostics • Alerts_\n\n"
    "### 🚀 Getting Started\n"
    "• **/price BTC** — current price in USD (e.g., `/price ETH`).\n"
    "• **/setalert BTC > 110000** — alert when condition is met.\n"
    "• **/myalerts** — list your active alerts.\n"
    "• **/help** — full instructions.\n\n"
    "💎 Premium: unlimited alerts. Free: up to 3."
)

HELP_TEXT = (
    "📘 **Crypto Alerts Bot — Help**\n\n"
    "### 🔧 Commands\n"
    "• **/price `<SYMBOL>`**, **/diagprice `<SYMBOL>`**\n"
    "• **/setalert `<SYMBOL>` `< > | < >` `<PRICE>`**, **/myalerts**, **/delalert `<ID>`**, **/clearalerts**\n"
    "• **/premium** — Check your plan & upgrade.\n"
    "• **/stats**, **/subs**, **/whoami**, **/bindsub**, **/syncsub** — admin/diagnostics.\n\n"
    "### ⏰ Alerts\n"
    "• One-shot, Free=3, Premium=unlimited. Checks ~1 min via /cron.\n"
)

def help_keyboard(uid: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("💎 Upgrade with PayPal", url=f"{PAYPAL_SUBSCRIBE_PAGE}?uid={uid}")]])

def quick_reply_keyboard():
    rows = [[KeyboardButton("/price BTC"), KeyboardButton("/price ETH")],
            [KeyboardButton("/setalert BTC > 110000"), KeyboardButton("/myalerts")]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, selective=True)

# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=help_keyboard(uid))
    try: await update.message.reply_text("⌨️ Quick actions:", reply_markup=quick_reply_keyboard())
    except Exception: pass

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; ensure_user(uid)
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=help_keyboard(uid))

async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; ensure_user(uid)
    status = "🌟 **Premium** (unlimited alerts)" if is_premium(uid) else "🆓 **Free** (up to 3 active alerts)"
    await update.message.reply_text(f"{status}\nUpgrade here: {PAYPAL_SUBSCRIBE_PAGE}", parse_mode="Markdown", reply_markup=help_keyboard(uid))

# --- Admin / Diagnostics ---
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram user id: {update.effective_user.id}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Not authorized."); return
    total_users   = CONN.execute("SELECT COUNT(*) FROM users").fetchone()[0] or 0
    premium_users = CONN.execute("SELECT COUNT(*) FROM users WHERE premium_active=1").fetchone()[0] or 0
    active_alerts = CONN.execute("SELECT COUNT(*) FROM alerts WHERE active=1").fetchone()[0] or 0
    subs_total    = CONN.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] or 0
    rows = CONN.execute("""
        SELECT UPPER(TRIM(COALESCE(status,''))) AS s, COUNT(*) 
        FROM subscriptions 
        GROUP BY s
    """).fetchall()
    by_status = { (s or 'UNKNOWN') : c for (s,c) in rows }
    subs_active = by_status.get('ACTIVE', 0)
    breakdown_lines = "  • " + "\n  • ".join([f"{k}={v}" for k,v in sorted(by_status.items())]) if rows else "  • (none)"
    await update.message.reply_text(
        "📊 **Bot Stats**\n\n"
        f"👥 Users: {total_users}\n"
        f"💎 Premium users: {premium_users}\n"
        f"🔔 Active alerts: {active_alerts}\n"
        f"🧾 Subscriptions: total={subs_total}, ACTIVE={subs_active}\n"
        f"{breakdown_lines}",
        parse_mode="Markdown"
    )

async def subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Not authorized."); return
    rows = CONN.execute("SELECT subscription_id,user_id,status,plan_id,last_event FROM subscriptions ORDER BY last_event DESC LIMIT 15").fetchall()
    if not rows:
        await update.message.reply_text("No subscriptions in DB."); return
    lines = ["🧾 **Recent subscriptions**"]
    for (sid, uid, st, plan, ts) in rows:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        lines.append(f"• {sid} | user={uid} | {st} | plan={plan} | {when}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def bindsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Not authorized."); return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /bindsub <SUB_ID> <USER_ID>"); return
    sub_id = context.args[0]; uid = int(context.args[1])
    set_subscription_record(sub_id, uid, status="BIND_ONLY")
    await update.message.reply_text(f"Bound {sub_id} → user {uid}. Now run /syncsub {sub_id}")

async def syncsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Not authorized."); return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /syncsub <SUB_ID>"); return
    sub_id = context.args[0]
    row = CONN.execute("SELECT user_id,status FROM subscriptions WHERE subscription_id=?", (sub_id,)).fetchone()
    if not row:
        await update.message.reply_text("Unknown subscription id in DB. Use /bindsub first."); return
    uid, status = row
    if status == "ACTIVE" and uid:
        set_premium(uid, True)
        await update.message.reply_text(f"User {uid} set to Premium (status ACTIVE).")
    else:
        await update.message.reply_text(f"Sub {sub_id}: user={uid}, status={status}. If you paid, check subscribe-bind & webhook.")

# ---------- Prices ----------
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Usage: `/price BTC`", parse_mode="Markdown"); return
    coin = normalize_symbol(context.args[0]); cg_id = SYMBOL_TO_ID.get(coin.lower(), coin.lower())
    p = resolve_price_usd(coin)
    if p is None: await update.message.reply_text("❌ Coin not found or API unavailable. Please try again."); return
    ts = PRICE_CACHE.get(cg_id, (None, 0))[1]; age = time.time() - ts; suffix = " *(stale)*" if age > CACHE_TTL else ""
    await update.message.reply_text(f"💰 **{coin.upper()}** price: **${p:.6f}**{suffix}", parse_mode="Markdown")

async def diagprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Usage: `/diagprice ETH`", parse_mode="Markdown"); return
    coin = normalize_symbol(context.args[0]); cg_id = SYMBOL_TO_ID.get(coin.lower(), coin.lower())
    cached = PRICE_CACHE.get(cg_id); cache_line = "Cache: none"
    if cached: age = int(time.time() - cached[1]); cache_line = f"Cache: {cached[0]} (age {age}s)"
    b = binance_price_for_symbol(coin); cg = cg_simple_price(cg_id); cg_price = None
    if cg and cg_id in cg and "usd" in cg[cg_id]: cg_price = cg[cg_id]["usd"]
    cc = coincap_price(cg_id); ccx = cryptocompare_price(coin)
    text = ("🔎 **Diagnostic**\n"
            f"Coin: **{coin.upper()}**  *(cg_id: {cg_id})*\n"
            f"{cache_line}\n• Binance: {b}\n• CoinGecko: {cg_price}\n• CoinCap: {cc}\n• CryptoCompare: {ccx}\n")
    await update.message.reply_text(text, parse_mode="Markdown")

# ---------- Alerts (Premium enforced: Free=3, Premium=∞) ----------
ALERT_USAGE = "Usage: `/setalert BTC > 110000`  or  `/setalert ETH < 2300`"
def parse_setalert(args):
    if len(args) < 3: return None
    sym = normalize_symbol(args[0]); op = args[1]
    if op not in (">","<"): return None
    try: thr = float(args[2].replace(",",""))
    except Exception: return None
    return (sym, op, thr)

async def setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; ensure_user(uid)
    if len(context.args) < 3: await update.message.reply_text(ALERT_USAGE, parse_mode="Markdown"); return
    parsed = parse_setalert(context.args)
    if not parsed: await update.message.reply_text(ALERT_USAGE, parse_mode="Markdown"); return
    sym, op, thr = parsed
    if sym.lower() not in SYMBOL_TO_ID: await update.message.reply_text("❌ Unknown symbol. Try BTC/ETH/SOL…"); return
    if not is_premium(uid):
        cnt = CONN.execute("SELECT COUNT(*) FROM alerts WHERE user_id=? AND active=1", (uid,)).fetchone()[0]
        if cnt >= 3:
            await update.message.reply_text("Free plan limit reached (3 alerts). Upgrade for unlimited alerts.", reply_markup=help_keyboard(uid)); return
    CONN.execute("INSERT INTO alerts(user_id,symbol,op,threshold,active,created_at) VALUES(?,?,?,?,1,?)",
                 (uid, sym.lower(), op, thr, time.time()))
    CONN.commit()
    await update.message.reply_text(f"✅ Alert saved: `{sym.upper()} {op} {thr}`", parse_mode="Markdown")

async def myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; ensure_user(uid)
    rows = CONN.execute("SELECT id,symbol,op,threshold,active FROM alerts WHERE user_id=? AND active=1 ORDER BY id DESC",(uid,)).fetchall()
    if not rows: await update.message.reply_text("You have no active alerts.\n`/setalert BTC > 110000`", parse_mode="Markdown"); return
    lines = ["🔔 **Your Alerts**"] + [f"• `{aid}` — **{s.upper()} {op} {thr}**" for (aid,s,op,thr,act) in rows]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; ensure_user(uid)
    if not context.args: await update.message.reply_text("Usage: `/delalert <ID>`", parse_mode="Markdown"); return
    try: aid = int(context.args[0])
    except Exception: await update.message.reply_text("Usage: `/delalert <ID>`", parse_mode="Markdown"); return
    cur = CONN.execute("UPDATE alerts SET active=0 WHERE id=? AND user_id=?", (aid, uid)); CONN.commit()
    await update.message.reply_text("🗑️ Deleted." if cur.rowcount else "Alert not found.", parse_mode="Markdown")

async def clearalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; ensure_user(uid)
    cur = CONN.execute("UPDATE alerts SET active=0 WHERE user_id=? AND active=1", (uid,)); CONN.commit()
    await update.message.reply_text(f"🧹 Cleared {cur.rowcount} alert(s).")

# =============== Polling helper (local) ===============
def run_bot():
    async def _post_init(application):
        try: await application.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e: logging.warning("delete_webhook failed %s", e)
    app = (Application.builder().token(BOT_TOKEN).post_init(_post_init).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("subs", subs))
    app.add_handler(CommandHandler("bindsub", bindsub))
    app.add_handler(CommandHandler("syncsub", syncsub))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("diagprice", diagprice))
    app.add_handler(CommandHandler("setalert", setalert))
    app.add_handler(CommandHandler("myalerts", myalerts))
    app.add_handler(CommandHandler("delalert", delalert))
    app.add_handler(CommandHandler("clearalerts", clearalerts))
    logging.info("🤖 Bot running (polling)…")
    app.run_polling(drop_pending_updates=True)

def get_db_conn(): return CONN

__all__ = [
    "start","help_cmd","premium_cmd","whoami","stats","subs","bindsub","syncsub",
    "price","diagprice","setalert","myalerts","delalert","clearalerts",
    "resolve_price_usd","normalize_symbol","SYMBOL_TO_ID",
    "get_db_conn","is_premium","ensure_user","set_premium","set_subscription_record",
    "set_premium_until","check_premium_expirations_now",
]

if __name__ == "__main__":
    run_bot()
