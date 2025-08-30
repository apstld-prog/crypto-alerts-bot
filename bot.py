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

# Public subscribe page (live ή sandbox)
PAYPAL_SUBSCRIBE_PAGE = os.getenv("PAYPAL_SUBSCRIBE_PAGE", "https://crypto-alerts-bot-k8i7.onrender.com/subscribe.html")
DB_PATH = os.getenv("DB_PATH", "bot.db")

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"

logging.basicConfig(level=logging.INFO)

# ================== CACHE / RETRIES ==================
PRICE_CACHE = {}            # cg_id -> (price_float, timestamp)
CACHE_TTL = 60.0            # fresh cache window (seconds)
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

# ================== SYMBOL MAP & NORMALIZE ==================
SYMBOL_TO_ID = {
    "btc":"bitcoin","eth":"ethereum","sol":"solana","bnb":"binancecoin",
    "xrp":"ripple","ada":"cardano","doge":"dogecoin","matic":"polygon",
    "trx":"tron","avax":"avalanche-2","dot":"polkadot","ltc":"litecoin",
    "usdt":"tether","usdc":"usd-coin","dai":"dai",
}
GREEK_TO_LATIN = str.maketrans({
    "Α":"A","Β":"B","Ε":"E","Ζ":"Z","Η":"H","Ι":"I","Κ":"K","Μ":"M","Ν":"N","Ο":"O","Ρ":"P","Τ":"T","Υ":"Y","Χ":"X",
    "α":"a","β":"b","ε":"e","ζ":"z","η":"h","ι":"i","κ":"k","μ":"m","ν":"n","ο":"o","ρ":"p","τ":"t","υ":"y","χ":"x",
})
def normalize_symbol(s: str) -> str:
    s = s.strip().translate(GREEK_TO_LATIN)
    s = re.sub(r"[^0-9A-Za-z\-]", "", s)
    return s.lower()

# ================== DB (users μόνο για τώρα) ==================
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        premium_active INTEGER DEFAULT 0,
        premium_until TEXT
    )""")
    conn.commit()
    return conn
CONN = db()

# ================== PROVIDERS (με retries) ==================
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
                price = data.get("price")
                if price is not None:
                    return float(price)
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

# ================== RESOLVER (4 πάροχοι + cache + stale) ==================
def resolve_price_usd(symbol: str):
    cg_id = SYMBOL_TO_ID.get(symbol.lower(), symbol.lower())
    now = time.time()
    cached = PRICE_CACHE.get(cg_id)
    if cached and now - cached[1] <= CACHE_TTL:
        return cached[0]

    p = binance_price_for_symbol(symbol)
    if p is not None:
        PRICE_CACHE[cg_id] = (p, now)
        return p

    data = cg_simple_price(cg_id)
    if cg_id in data and "usd" in data[cg_id]:
        p = float(data[cg_id]["usd"])
        PRICE_CACHE[cg_id] = (p, now)
        return p

    p3 = coincap_price(cg_id)
    if p3 is not None:
        PRICE_CACHE[cg_id] = (p3, now)
        return p3

    p4 = cryptocompare_price(symbol)
    if p4 is not None:
        PRICE_CACHE[cg_id] = (p4, now)
        return p4

    if cached and now - cached[1] <= STALE_TTL:
        return cached[0]
    return None

# ================== UI ELEMENTS ==================
WELCOME_TEXT = (
    "🪙 **Crypto Alerts Bot**\n"
    "_Fast prices • Diagnostics • (Upcoming) Alerts_\n\n"
    "### 🚀 Getting Started\n"
    "• **/price BTC** — current price in USD (e.g., `/price ETH`).\n"
    "• **/diagprice BTC** — provider diagnostics & cache info.\n"
    "• **/help** — full instructions & tips.\n\n"
    "💎 Upgrade to support development & unlock upcoming premium features."
)

HELP_TEXT = (
    "📘 **Crypto Alerts Bot — Help**\n\n"
    "### 🔧 Commands\n"
    "• **/price `<SYMBOL>`** — Get current price in USD.\n"
    "  _Examples:_ ` /price BTC`, ` /price eth`, ` /price sol`\n"
    "• **/diagprice `<SYMBOL>`** — See diagnostic info per provider (Binance, CoinGecko, CoinCap, CryptoCompare) and cache status.\n"
    "• **/help** — Show this help.\n\n"
    "### 🧠 Tips\n"
    "• Symbols are case-insensitive: `btc`, `ETH`, `Sol` all work.\n"
    "• If you see **(stale)**, live quotes were temporarily unavailable; I showed the last known price (≤ 5 min old).\n"
    "• Supported majors (for now): BTC, ETH, SOL, BNB, XRP, ADA, DOGE, MATIC, TRX, AVAX, DOT, LTC, USDT, USDC, DAI.\n\n"
    "### ⏰ Alerts (Overview)\n"
    "You’ll be able to set alerts like:\n"
    "• **Above price** — _Notify me when_ `BTC` **> 70,000 USD**\n"
    "• **Below price** — _Notify me when_ `ETH` **< 2,300 USD**\n"
    "• **Percent moves** — _Notify me if_ `SOL` **±5%** in 1h\n\n"
    "**How it will work (UI):**\n"
    "• Command: `/setalert BTC > 70000`  or  `/setalert ETH < 2300`\n"
    "• List alerts: `/myalerts`\n"
    "• Remove: `/delalert <id>`  or  `/clearalerts`\n\n"
    "🟣 *Premium plan* will include multi-coin alerts, tighter intervals, daily/weekly summaries.\n\n"
    "### 🔐 Premium\n"
    "Tap **Upgrade with PayPal** to support development & unlock upcoming premium features.\n"
    "If you need help, just reply here with your question."
)

def help_keyboard(uid: int):
    # Inline buttons: Upgrade + Quick links
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Upgrade with PayPal", url=f"{PAYPAL_SUBSCRIBE_PAGE}?uid={uid}")],
        [
            InlineKeyboardButton("ℹ️ Help", callback_data="noop_help"),
            InlineKeyboardButton("🧪 Diagnostics", callback_data="noop_diag")
        ]
    ])

def quick_reply_keyboard():
    # Optional: Reply keyboard with shortcuts (user can hide it)
    rows = [
        [KeyboardButton("/price BTC"), KeyboardButton("/price ETH")],
        [KeyboardButton("/diagprice BTC"), KeyboardButton("/help")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, selective=True)

# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        CONN.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
        CONN.commit()
    except Exception:
        pass
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="Markdown",
        reply_markup=help_keyboard(uid)
    )
    # Προαιρετικά δώσε και quick reply keyboard με βασικά commands
    try:
        await update.message.reply_text(
            "⌨️ Quick actions:",
            reply_markup=quick_reply_keyboard()
        )
    except Exception:
        pass

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode="Markdown",
        reply_markup=help_keyboard(uid)
    )

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/price BTC`", parse_mode="Markdown")
        return
    coin = normalize_symbol(context.args[0])
    cg_id = SYMBOL_TO_ID.get(coin.lower(), coin.lower())

    p = resolve_price_usd(coin)
    if p is None:
        await update.message.reply_text("❌ Coin not found or API unavailable. Please try again shortly.")
        return

    ts = PRICE_CACHE.get(cg_id, (None, 0))[1]
    age = time.time() - ts
    suffix = " *(stale)*" if age > CACHE_TTL else ""
    await update.message.reply_text(
        f"💰 **{coin.upper()}** price: **${p:.6f}**{suffix}",
        parse_mode="Markdown"
    )

async def diagprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/diagprice ETH`", parse_mode="Markdown")
        return
    coin = normalize_symbol(context.args[0])
    cg_id = SYMBOL_TO_ID.get(coin.lower(), coin.lower())

    # cache
    cached = PRICE_CACHE.get(cg_id)
    cache_line = "Cache: none"
    if cached:
        age = int(time.time() - cached[1])
        cache_line = f"Cache: {cached[0]} (age {age}s)"

    # live providers
    b = binance_price_for_symbol(coin)
    cg = cg_simple_price(cg_id)
    cg_price = None
    if cg and cg_id in cg and "usd" in cg[cg_id]:
        cg_price = cg[cg_id]["usd"]
    cc = coincap_price(cg_id)
    ccx = cryptocompare_price(coin)

    text = (
        "🔎 **Diagnostic**\n"
        f"Coin: **{coin.upper()}**  *(cg_id: {cg_id})*\n"
        f"{cache_line}\n"
        f"• Binance: {b}\n"
        f"• CoinGecko: {cg_price}\n"
        f"• CoinCap: {cc}\n"
        f"• CryptoCompare: {ccx}\n"
        "\nTip: If all live providers fail intermittently, you’ll still see a **stale** value for up to 5 minutes."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# =============== (Optional) Standalone run ===============
def run_bot():
    """
    Αν το τρέχεις μόνο του σε polling mode (π.χ. τοπικά).
    Στο Render με webhook, το server_combined.py κάνει import τους handlers.
    """
    from telegram.ext import Application
    async def _post_init(application):
        try:
            await application.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logging.warning("delete_webhook failed %s", e)

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
    logging.info("🤖 Bot running (polling)…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    run_bot()
