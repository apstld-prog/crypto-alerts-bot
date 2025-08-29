#!/usr/bin/env python3
import logging, os, sqlite3, re, time
from datetime import datetime, timedelta
import requests
from collections import deque

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, JobQueue

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("Missing or invalid BOT_TOKEN env var")

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_SEARCH = "https://api.coingecko.com/api/v3/search"
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "60"))
PAYPAL_SUBSCRIBE_PAGE = os.getenv("PAYPAL_SUBSCRIBE_PAGE", "https://crypto-alerts-bot-k8i7.onrender.com/subscribe.html")
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
    "bch": "bitcoin-cash",
    "etc": "ethereum-classic",
    "usdt": "tether",
    "usdc": "usd-coin",
    "dai": "dai",
    "cro": "crypto-com-chain"
}
_SYMBOL_CACHE = {}

# Normalize
GREEK_TO_LATIN = str.maketrans({
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    "α": "a", "β": "b", "ε": "e", "ζ": "z", "η": "h", "ι": "i", "κ": "k",
    "μ": "m", "ν": "n", "ο": "o", "ρ": "p", "τ": "t", "υ": "y", "χ": "x",
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
def binance_price_for_symbol(symbol_or_id: str):
    sym = symbol_or_id.upper()
    mapping = {
        "bitcoin": "BTC",
        "ethereum": "ETH",
        "solana": "SOL",
        "ripple": "XRP",
        "cardano": "ADA",
        "dogecoin": "DOGE"
    }
    if sym not in mapping.values():
        sym = mapping.get(symbol_or_id.lower(), sym)
    pair = sym + "USDT"
    _throttle()
    try:
        r = requests.get("https://api.binance
