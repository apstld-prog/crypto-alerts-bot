#!/usr/bin/env python3
import logging, os, sqlite3, re, time, random
import requests
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("Missing or invalid BOT_TOKEN env var")

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"
PAYPAL_SUBSCRIBE_PAGE = os.getenv("PAYPAL_SUBSCRIBE_PAGE", "https://crypto-alerts-bot-k8i7.onrender.com/subscribe.html")
DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(level=logging.INFO)

# ========== CACHE / RETRIES ==========
PRICE_CACHE = {}            # key: cg_id, value: (price_float, ts)
CACHE_TTL = 60.0            # live cache window (seconds)
STALE_TTL = 300.0           # allow stale (seconds)
RETRY_MAX = 3
RETRY_SLEEP = 0.35

_LAST_CALLS = deque(maxlen=12)

def _throttle():
    now = time.time()
    if _LAST_CALLS and now - _LAST_CALLS[-1] < 0.35:
        time.sleep(0.35 - (now - _LAST_CALLS[-1]))
    _LAST_CALLS.append(time.time())

def _sleep_jitter(base):
    time.sleep(base + random.uniform(0, 0.15))

# ========== SYMBOL → COINGECKO ID ==========
SYMBOL_TO_ID = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "bnb": "binancecoin",
    "xrp": "ripple", "ada": "cardano", "doge": "dogecoin", "matic": "polygon",
    "trx": "tron", "avax": "avalanche-2", "dot": "polkadot", "ltc": "litecoin",
    "usdt": "tether", "usdc": "usd-coin", "dai": "dai",
}

# Normalize (Greek → Latin lookalikes + strip)
GREEK_TO_LATIN = str.maketrans({
    "Α":"A","Β":"B","Ε":"E","Ζ":"Z","Η":"H","Ι":"I","Κ":"K","Μ":"M","Ν":"N","Ο":"O","Ρ":"P","Τ":"T","Υ":"Y","Χ":"X",
    "α":"a","β":"b","ε":"e","ζ":"z","η":"h","ι":"i","κ":"k","μ":"m","ν":"n","ο":"o","ρ":"p","τ":"t","υ":"y","χ":"x",
})
def normalize_symbol(s: str) -> str:
    s = s.strip().translate(GREEK_TO_LATIN)
    s = re.sub(r"[^0-9A-Za-z\-]", "", s)
    return s.lower()

# ========== DB (users + leader lock) ==========
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        premium_active INTEGER DEFAULT 0,
        premium_until TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS leader(
        lock INTEGER PRIMARY KEY CHECK(lock=1),
        run_id TEXT,
        ts REAL
    )""")
    conn.commit()
    return conn
CONN = db()

RUN_ID = os.getenv("RENDER_INSTANCE_ID") or f"pid-{os.getpid()}"

def acquire_leader_lock(max_stale=600.0) -> bool:
    now = time.time()
    try:
        CONN.execute("INSERT INTO leader(lock, run_id, ts) VALUES(1, ?, ?)", (RUN_ID, now))
        CONN.commit()
        logging.info("Leader lock acquired by %s", RUN_ID)
        return True
    except sqlite3.IntegrityError:
        row = CONN.execute("SELECT run_id, ts FROM leader WHERE lock=1").fetchone()
        if not row:
            return False
        old_run, ts = row
        if now - float(ts or 0) > max_stale:
            logging.warning("Leader lock stale (owner=%s, age=%.0fs). Taking over.", old_run, now - float(ts or 0))
            CONN.execute("UPDATE leader SET run_id=?, ts=?", (RUN_ID, now))
            CONN.commit()
            return True
        logging.info("Leader lock held by %s (age %.0fs). Not starting polling.", old_run, now - float(ts or 0))
        return False

def heartbeat_leader():
    try:
        CONN.execute("UPDATE leader SET ts=? WHERE lock=1 AND run_id=?", (time.time(), RUN_ID))
        CONN.commit()
    except Exception as e:
        logging.warning("Leader heartbeat failed: %s", e)

# ========== PROVIDERS ==========
def binance_price_for_symbol(symbol_or_id: str):
    sym = symbol_or_id.upper()
    cg_map = {
        "bitcoin":"BTC","ethereum":"ETH","solana":"SOL","ripple":"XRP","cardano":"ADA",
        "dogecoin":"DOGE","polygon":"MATIC","tron":"TRX","avalanche-2":"AVAX","polkadot":"DOT","litecoin":"LTC"
    }
    if sym not in cg_map.values():
        sym = cg_map.get(symbol_or_id.lower(), sym)
    pair = sym + "USDT"

    hosts = [
        "https://api.binance.com",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api3.binance.com",
    ]

    for attempt in range(RETRY_MAX):
        _throttle()
        host = hosts[attempt % len(hosts)]
        try:
            r = requests.get(f"{host}/api/v3/ticker/price", params={"symbol": pair}, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if "price" in data:
                    return float(data["price"])
        except Exception:
            pass
        _sleep_jitter(RETRY_SLEEP)
    return None

def cg_simple_price(ids_csv: str) -> dict:
    for _ in range(RETRY_MAX):
        _throttle()
        try:
            r = requests.get(COINGECKO_SIMPLE, params={"ids": ids_csv, "vs_currencies": "usd"}, timeout=8)
            if r.status_code == 200:
                return r.json() or {}
        except Exception:
            pass
        _sleep_jitter(RETRY_SLEEP)
    return {}

def coincap_price(cg_id: str):
    for _ in range(RETRY_MAX):
        _throttle()
        try:
            r = requests.get(f"https://api.coincap.io/v2/assets/{cg_id}", timeout=8)
            if r.status_code == 200:
                data = r.json()
                price = data.get("data", {}).get("priceUsd")
                if price is not None:
                    return float(price)
        except Exception:
            pass
        _sleep_jitter(RETRY_SLEEP)
    return None

def cryptocompare_price(symbol_or_id: str):
    sym = symbol_or_id.upper()
    for _ in range(RETRY_MAX):
        _throttle()
        try:
            r = requests.get(
                "https://min-api.cryptocompare.com/data/price",
                params={"fsym": sym, "tsyms": "USD"},
                timeout=8
            )
            if r.status_code == 200:
                data = r.json()
                if "USD" in data:
                    return float(data["USD"])
        except Exception:
            pass
        _sleep_jitter(RETRY_SLEEP)
    return None

# ========== RESOLVER ==========
PRICE_CACHE = {}  # (moved here to be above resolve if needed by linters)

def resolve_price_usd(symbol: str):
    cg_id = SYMBOL_TO_ID.get(symbol.lower(), symbol.lower())

    cached = PRICE_CACHE.get(cg_id)
    now = time.time()
    if cached and now - cached[1] <= CACHE_TTL:
        return cached[0]

    # 1) Binance
    p = binance_price_for_symbol(symbol)
    if p is not None:
        PRICE_CACHE[cg_id] = (p, now)
        return p

    # 2) CoinGecko
    data = cg_simple_price(cg_id)
    if cg_id in data and "usd" in data[cg_id]:
        p = float(data[cg_id]["usd"])
        PRICE_CACHE[cg_id] = (p, now)
        return p

    # 3) CoinCap
    p3 = coincap_price(cg_id)
    if p3 is not None:
        PRICE_CACHE[cg_id] = (p3, now)
        return p3

    # 4) CryptoCompare
    p4 = cryptocompare_price(symbol)
    if p4 is not None:
        PRICE_CACHE[cg_id] = (p4, now)
        return p4

    # 5) Stale cache fallback
    if cached and now - cached[1] <= STALE_TTL:
        return cached[0]

    return None

# ========== HELP TEXT ==========
HELP_TEXT = (
    "👋 *Welcome to Crypto Alerts Bot!*\n\n"
    "Here’s how to use me:\n"
    "• `/price BTC` — Get the current price in USD (e.g., `/price ETH`).\n"
    "• `/diagprice BTC` — Diagnostic: shows which data providers responded and cache info.\n\n"
    "Tips:\n"
    "• Symbols are case-insensitive (`btc`, `ETH`, etc.).\n"
    "• If you see the label `(stale)`, a fresh quote wasn’t available; I showed the last known price (≤5 min old).\n"
    "• Supported majors include: BTC, ETH, SOL, BNB, XRP, ADA, DOGE, MATIC, TRX, AVAX, DOT, LTC, USDT, USDC, DAI.\n\n"
    "Premium (optional):\n"
    "• Tap *Upgrade with PayPal* to support development & unlock upcoming features.\n"
    "• Roadmap: multi-coin alerts, daily signals, weekly recap.\n"
)

def help_keyboard(user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Upgrade with PayPal", url=f"{PAYPAL_SUBSCRIBE_PAGE}?uid={user_id}")],
    ])

# ========== HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        CONN.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
        CONN.commit()
    except Exception:
        pass
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=help_keyboard(uid))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=help_keyboard(uid))

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price BTC")
        return
    coin = normalize_symbol(context.args[0])
    cg_id = SYMBOL_TO_ID.get(coin.lower(), coin.lower())

    p = resolve_price_usd(coin)
    if p is None:
        await update.message.reply_text("❌ Coin not found or API unavailable.")
        return

    ts = PRICE_CACHE.get(cg_id, (None, 0))[1]
    age = time.time() - ts
    suffix = " (stale)" if age > CACHE_TTL else ""
    await update.message.reply_text(f"💰 {coin.upper()} price: ${p}{suffix}")

async def diagprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /diagprice ETH")
        return
    coin = normalize_symbol(context.args[0])
    cg_id = SYMBOL_TO_ID.get(coin.lower(), coin.lower())

    cached = PRICE_CACHE.get(cg_id)
    cache_line = "Cache: none"
    if cached:
        age = int(time.time() - cached[1])
        cache_line = f"Cache: {cached[0]} (age {age}s)"

    b = binance_price_for_symbol(coin)
    cg = cg_simple_price(cg_id)
    cg_price = None
    if cg and cg_id in cg and "usd" in cg[cg_id]:
        cg_price = cg[cg_id]["usd"]
    cc = coincap_price(cg_id)
    ccx = cryptocompare_price(coin)

    text = (
        "🔎 Diagnostic\n"
        f"Coin: {coin} (cg_id: {cg_id})\n"
        f"{cache_line}\n"
        f"Binance: {b}\n"
        f"CoinGecko: {cg_price}\n"
        f"CoinCap: {cc}\n"
        f"CryptoCompare: {ccx}"
    )
    await update.message.reply_text(text)

# ========== BOOT ==========
def run_bot():
    async def _post_init(application):
        try:
            await application.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logging.warning("delete_webhook failed %s", e)

    # Single-runner protection
    def try_leader():
        if not acquire_leader_lock():
            logging.warning("Another instance holds the leader lock. Exiting bot startup to avoid conflicts.")
            return False
        return True

    if not try_leader():
        return

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("diagprice", diagprice))

    logging.info("🤖 Bot running… (leader: %s)", RUN_ID)

    # Heartbeat leader lock
    import threading
    def _hb():
        while True:
            heartbeat_leader()
            time.sleep(30)
    threading.Thread(target=_hb, daemon=True).start()

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run_bot()
