#!/usr/bin/env python3
import logging, os, sqlite3, re, time
from datetime import datetime, timedelta
import requests
from collections import deque

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("Missing or invalid BOT_TOKEN env var")

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "60"))
PAYPAL_SUBSCRIBE_PAGE = os.getenv(
    "PAYPAL_SUBSCRIBE_PAGE", "https://crypto-alerts-bot-k8i7.onrender.com/subscribe.html"
)
DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(level=logging.INFO)

# ========== SYMBOL → COINGECKO ID ==========
SYMBOL_TO_ID = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "bnb": "binancecoin",
    "xrp": "ripple",
    "ada": "cardano",
    "doge": "dogecoin",
    "matic": "polygon",
    "trx": "tron",
    "avax": "avalanche-2",
    "dot": "polkadot",
    "ltc": "litecoin",
    "usdt": "tether",
    "usdc": "usd-coin",
    "dai": "dai",
}
_SYMBOL_CACHE = {}

# Normalize (Greek to Latin)
GREEK_TO_LATIN = str.maketrans({
    "Α":"A","Β":"B","Ε":"E","Ζ":"Z","Η":"H","Ι":"I","Κ":"K",
    "Μ":"M","Ν":"N","Ο":"O","Ρ":"P","Τ":"T","Υ":"Y","Χ":"X",
    "α":"a","β":"b","ε":"e","ζ":"z","η":"h","ι":"i","κ":"k",
    "μ":"m","ν":"n","ο":"o","ρ":"p","τ":"t","υ":"y","χ":"x",
})
def normalize_symbol(s: str) -> str:
    s = s.strip().translate(GREEK_TO_LATIN)
    s = re.sub(r"[^0-9A-Za-z\-]", "", s)
    return s.lower()

# Cache + throttle
PRICE_CACHE = {}
CACHE_TTL = 30.0
_LAST_CALLS = deque(maxlen=10)
def _throttle():
    now = time.time()
    if _LAST_CALLS and now - _LAST_CALLS[-1] < 0.4:
        time.sleep(0.4 - (now - _LAST_CALLS[-1]))
    _LAST_CALLS.append(time.time())

# DB
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        premium_active INTEGER DEFAULT 0,
        premium_until TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        coin TEXT,
        target REAL
    )""")
    conn.commit()
    return conn
CONN = db()

# -------- Providers --------
def binance_price_for_symbol(symbol: str):
    sym = symbol.upper()
    mapping = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}
    if sym not in mapping.values():
        sym = mapping.get(symbol.lower(), sym)
    pair = sym + "USDT"
    _throttle()
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": pair}, timeout=10
        )
        if r.status_code != 200:
            return None
        return float(r.json().get("price"))
    except:
        return None

def cg_simple_price(ids: str):
    _throttle()
    try:
        r = requests.get(
            COINGECKO_SIMPLE,
            params={"ids": ids, "vs_currencies": "usd"},
            timeout=10
        )
        if r.status_code != 200:
            return {}
        return r.json()
    except:
        return {}

# -------- Resolver (Binance-first) --------
def resolve_price_usd(symbol: str):
    coin_id = SYMBOL_TO_ID.get(symbol.lower(), symbol.lower())
    cached = PRICE_CACHE.get(coin_id)
    if cached and time.time() - cached[1] <= CACHE_TTL:
        return cached[0]

    # 1) Binance
    p = binance_price_for_symbol(symbol)
    if p is not None:
        PRICE_CACHE[coin_id] = (p, time.time())
        return p

    # 2) CoinGecko
    data = cg_simple_price(coin_id)
    if coin_id in data:
        p = float(data[coin_id]["usd"])
        PRICE_CACHE[coin_id] = (p, time.time())
        return p

    return None

# -------- Handlers --------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    CONN.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
    CONN.commit()
    kb = [[InlineKeyboardButton("Upgrade with PayPal",
                                url=f"{PAYPAL_SUBSCRIBE_PAGE}?uid={uid}")]]
    await update.message.reply_text(
        "👋 Welcome to *Crypto Alerts Bot!*\n"
        "Use `/price BTC` to get prices.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price BTC")
        return
    coin = context.args[0]
    p = resolve_price_usd(coin)
    if p is None:
        await update.message.reply_text("❌ Coin not found or API unavailable.")
        return
    await update.message.reply_text(f"💰 {coin.upper()} price: ${p}")

async def diagprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /diagprice ETH")
        return
    coin = context.args[0]
    b = binance_price_for_symbol(coin)
    cg = cg_simple_price(SYMBOL_TO_ID.get(coin.lower(), coin.lower()))
    cg_price = None
    if cg and SYMBOL_TO_ID.get(coin.lower(), coin.lower()) in cg:
        cg_price = cg[SYMBOL_TO_ID.get(coin.lower(), coin.lower())]["usd"]
    await update.message.reply_text(
        f"🔎 {coin}\nBinance: {b}\nCoinGecko: {cg_price}"
    )

# -------- Boot --------
def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("diagprice", diagprice))

    async def _post_init(app):
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logging.warning("delete_webhook failed %s", e)

    app.post_init(_post_init)
    logging.info("🤖 Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run_bot()
