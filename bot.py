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
ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit())

logging.basicConfig(level=logging.INFO)

# ========== SYMBOL → COINGECKO ID ==========
SYMBOL_TO_ID = {
    "btc":"bitcoin","eth":"ethereum","sol":"solana","bnb":"binancecoin","xrp":"ripple","ada":"cardano",
    "doge":"dogecoin","matic":"polygon","trx":"tron","avax":"avalanche-2","dot":"polkadot","ltc":"litecoin",
    "bch":"bitcoin-cash","etc":"ethereum-classic","xlm":"stellar","xmr":"monero","atom":"cosmos","link":"chainlink",
    "uni":"uniswap","aave":"aave","comp":"compound-governance-token","snx":"synthetix-network-token",
    "sand":"the-sandbox","mana":"decentraland","axs":"axie-infinity","ape":"apecoin",
    "usdt":"tether","usdc":"usd-coin","dai":"dai","cro":"crypto-com-chain"
}
_SYMBOL_CACHE = {}

# Normalize
GREEK_TO_LATIN = str.maketrans({"Α":"A","Β":"B","Ε":"E","Ζ":"Z","Η":"H","Ι":"I","Κ":"K","Μ":"M","Ν":"N","Ο":"O","Ρ":"P","Τ":"T","Υ":"Y","Χ":"X",
                                "α":"a","β":"b","ε":"e","ζ":"z","η":"h","ι":"i","κ":"k","μ":"m","ν":"n","ο":"o","ρ":"p","τ":"t","υ":"y","χ":"x"})
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
    conn.execute("""CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,premium_active INTEGER DEFAULT 0,premium_until TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,coin TEXT,target REAL)""")
    conn.commit()
    return conn
CONN = db()

def set_premium(user_id: int, days: int = 31):
    until = (datetime.utcnow() + timedelta(days=days)).isoformat()
    CONN.execute("INSERT INTO users(user_id,premium_active,premium_until) VALUES(?,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET premium_active=excluded.premium_active,premium_until=excluded.premium_until",
                 (user_id,1,until)); CONN.commit()

def is_premium(user_id: int) -> bool:
    cur = CONN.execute("SELECT premium_active,premium_until FROM users WHERE user_id=?",(user_id,))
    row = cur.fetchone()
    if not row:
