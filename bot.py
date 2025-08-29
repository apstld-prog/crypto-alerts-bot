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

# ========== SYMBOL → COINGECKO ID (extended base set) ==========
SYMBOL_TO_ID = {
    # Layer1/Layer2 majors
    "btc":"bitcoin","eth":"ethereum","sol":"solana","bnb":"binancecoin","xrp":"ripple","ada":"cardano",
    "doge":"dogecoin","matic":"polygon","trx":"tron","avax":"avalanche-2","dot":"polkadot","ltc":"litecoin",
    "bch":"bitcoin-cash","etc":"ethereum-classic","xlm":"stellar","xmr":"monero","atom":"cosmos","link":"chainlink",
    "near":"near","apt":"aptos","sui":"sui","arb":"arbitrum","op":"optimism","imx":"immutable-x","kas":"kaspa",
    "icp":"internet-computer","egld":"multiversx","fil":"filecoin","hbar":"hedera-hashgraph","algo":"algorand",
    "vet":"vechain","theta":"theta-token","grt":"the-graph","ftm":"fantom","stx":"stacks","inj":"injective-protocol",
    "ldo":"lido-dao","rune":"thorchain","sei":"sei-network","tia":"celestia","rose":"oasis-network",
    "one":"harmony","neo":"neo","ksm":"kusama","xdc":"xdce-crowd-sale","icx":"icon","ton":"the-open-network",
    # DeFi & DEX
    "uni":"uniswap","aave":"aave","comp":"compound-governance-token","snx":"synthetix-network-token",
    "crv":"curve-dao-token","sushi":"sushi","yfi":"yearn-finance","bal":"balancer","cake":"pancakeswap-token",
    "1inch":"1inch","cvx":"convex-finance","gmx":"gmx","dydx":"dydx","joe":"joe","kswap":"kyber-network-crystal",
    "blur":"blur","pendle":"pendle","rndr":"render-token","mpl":"maple","fxs":"frax-share",
    # NFT/Gaming/Metaverse
    "sand":"the-sandbox","mana":"decentraland","axs":"axie-infinity","enj":"enjincoin","ape":"apecoin",
    "gmt":"stepn","beam":"beam-2","ron":"ronin",
    # Oracles, infra, AI
    "ocean":"ocean-protocol","fet":"fetch-ai","agix":"singularitynet","tao":"bittensor","phb":"phoenix-global",
    "band":"band-protocol","api3":"api3","pyth":"pyth-network",
    # Payments & others / memes
    "xvg":"verge","dash":"dash","zec":"zcash","wif":"dogwifcoin","bonk":"bonk","pepe":"pepe","shib":"shiba-inu",
    "floki":"floki","safemoon":"safemoon","qnt":"quant-network","hnt":"helium","sfp":"safepal",
    # Stablecoins (reference)
    "usdt":"tether","usdc":"usd-coin","dai":"dai","tusd":"true-usd","usdd":"usdd","frax":"frax",
    # Exchanges/CEXs
    "okb":"okb","gt":"gatechain-token","leo":"leo-token","cro":"crypto-com-chain",
    # Wrapped/lsd
    "wbtc":"wrapped-bitcoin","weeth":"wrapped-eeth","steth":"staked-ether","reth":"rocket-pool-eth","wbnb":"wbnb",
    # Newer/popular
    "core":"coredaoorg","ena":"ethena","aevo":"aevo","alt":"altlayer","sfrxeth":"staked-frax-ether","wld":"worldcoin-wld"
}
_SYMBOL_CACHE = {}  # auto-filled via search

# Normalization (handles accidental Greek lookalikes, spaces, symbols)
GREEK_TO_LATIN = str.maketrans({
    "Α":"A","Β":"B","Ε":"E","Ζ":"Z","Η":"H","Ι":"I","Κ":"K","Μ":"M","Ν":"N","Ο":"O","Ρ":"P","Τ":"T","Υ":"Y","Χ":"X",
    "α":"a","β":"b","ε":"e","ζ":"z","η":"h","ι":"i","κ":"k","μ":"m","ν":"n","ο":"o","ρ":"p","τ":"t","υ":"y","χ":"x",
})
def normalize_symbol(s: str) -> str:
    s = s.strip().translate(GREEK_TO_LATIN)
    s = re.sub(r"[^0-9A-Za-z\-]", "", s)
    return s.lower()

# Caching & throttling for external calls
PRICE_CACHE = {}  # key: cg_id, value: (price_float, timestamp)
CACHE_TTL = 30.0  # seconds
_LAST_CALLS = deque(maxlen=10)

def _throttle():
    now = time.time()
    if _LAST_CALLS and now - _LAST_CALLS[-1] < 0.4:
        time.sleep(0.4 - (now - _LAST_CALLS[-1]))
    _LAST_CALLS.append(time.time())

def _search_coingecko_id(symbol: str) -> str | None:
    try:
        _throttle()
        r = requests.get(COINGECKO_SEARCH, params={"query": symbol}, timeout=8, headers={"User-Agent":"CryptoAlertsBot/1.0"})
        data = r.json()
        for c in data.get("coins", []):
            if c.get("symbol","").lower() == symbol.lower():
                return c["id"]
        if data.get("coins"):
            return data["coins"][0]["id"]
    except Exception:
        return None
    return None

def to_cg_id(symbol_or_id: str) -> str:
    s = normalize_symbol(symbol_or_id)
    if s in SYMBOL_TO_ID: return SYMBOL_TO_ID[s]
    if s in _SYMBOL_CACHE: return _SYMBOL_CACHE[s]
    if "-" in s or " " in s: return s
    found = _search_coingecko_id(s)
    if found:
        _SYMBOL_CACHE[s] = found
        return found
    return s

# ========== DB helpers ==========
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

def set_premium(user_id: int, days: int = 31):
    until = (datetime.utcnow() + timedelta(days=days)).isoformat()
    CONN.execute(
        "INSERT INTO users(user_id, premium_active, premium_until) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET premium_active=excluded.premium_active, premium_until=excluded.premium_until",
        (user_id, 1, until)
    ); CONN.commit()

def is_premium(user_id: int) -> bool:
    cur = CONN.execute("SELECT premium_active, premium_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row: return False
    active, until = row
    if not until: return bool(active)
    try:
        return bool(active) and datetime.fromisoformat(until) > datetime.utcnow()
    except Exception:
        return bool(active)

def add_alert(user_id: int, coin: str, target: float):
    CONN.execute("INSERT INTO alerts(user_id, coin, target) VALUES(?,?,?)", (user_id, coin.lower(), float(target))); CONN.commit()

def list_unique_coins():
    return [r[0] for r in CONN.execute("SELECT DISTINCT coin FROM alerts").fetchall()]

def user_alerts(user_id: int):
    return [(r[0], r[1]) for r in CONN.execute("SELECT coin, target FROM alerts WHERE user_id=?", (user_id,)).fetchall()]

def remove_alert(user_id: int, coin: str, target: float):
    CONN.execute("DELETE FROM alerts WHERE user_id=? AND coin=? AND target=?", (user_id, coin.lower(), float(target))); CONN.commit()

def parse_pairs(text_args):
    if not text_args: return []
    raw = text_args[0] if len(text_args)==1 else " ".join(text_args)
    raw = raw.replace(";", " ").replace(",", " ")
    toks = raw.split()
    out=[]; i=0
    while i < len(toks)-1:
        coin=toks[i].strip().lower(); price_str=toks[i+1].strip().rstrip(",;")
        try:
            out.append((normalize_symbol(coin), float(price_str))); i+=2
        except: i+=1
    return out

# ---------- Price providers (with throttle) ----------
def cg_simple_price(ids_csv: str) -> dict:
    _throttle()
    try:
        r = requests.get(
            COINGECKO_SIMPLE,
            params={"ids": ids_csv, "vs_currencies": "usd"},
            headers={"Accept": "application/json", "User-Agent": "CryptoAlertsBot/1.0"},
            timeout=10
        )
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception:
        return {}

def cg_market_price_single(cg_id: str):
    _throttle()
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": cg_id},
            headers={"Accept": "application/json", "User-Agent": "CryptoAlertsBot/1.0"},
            timeout=10
        )
        if r.status_code != 200:
            return None
        arr = r.json()
        if isinstance(arr, list) and arr:
            return float(arr[0].get("current_price"))
    except Exception:
        return None
    return None

def coincap_price(cg_id: str):
    _throttle()
    try:
        r = requests.get(
            f"https://api.coincap.io/v2/assets/{cg_id}",
            headers={"Accept": "application/json", "User-Agent": "CryptoAlertsBot/1.0"},
            timeout=10
        )
        if r.status_code != 200:
            return None
        data = r.json()
        price = data.get("data", {}).get("priceUsd")
        return float(price) if price is not None else None
    except Exception:
        return None

def binance_price_for_symbol(symbol_or_id: str):
    sym = symbol_or_id.upper()
    cg_to_ticker = {
        "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "ripple": "XRP",
        "cardano": "ADA", "dogecoin": "DOGE", "polygon": "MATIC", "tron": "TRX",
        "avalanche-2": "AVAX", "polkadot": "DOT", "litecoin": "LTC"
    }
    if sym not in cg_to_ticker.values():
        sym = cg_to_ticker.get(symbol_or_id.lower(), sym)
    pair = sym + "USDT"
    _throttle()
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": pair},
            headers={"Accept": "application/json", "User-Agent": "CryptoAlertsBot/1.0"},
            timeout=10
        )
        if r.status_code != 200:
            return None
        data = r.json()
        price = data.get("price")
        return float(price) if price is not None else None
    except Exception:
        return None

def resolve_price_usd(symbol_or_id: str):
    cg_id = to_cg_id(symbol_or_id)
    cached = PRICE_CACHE.get(cg_id)
    if cached and (time.time() - cached[1] <= CACHE_TTL):
        return cached[0]

    data = cg_simple_price(cg_id)
    if cg_id in data and "usd" in data[cg_id]:
        p = float(data[cg_id]["usd"])
        PRICE_CACHE[cg_id] = (p, time.time())
        return p

    p2 = cg_market_price_single(cg_id)
    if p2 is not None:
        PRICE_CACHE[cg_id] = (p2, time.time())
        return p2

    p3 = coincap_price(cg_id)
    if p3 is not None:
        PRICE_CACHE[cg_id] = (p3, time.time())
        return p3

    p4 = binance_price_for_symbol(symbol_or_id)
    if p4 is not None:
        PRICE_CACHE[cg_id] = (p4, time.time())
        return p4

    return None

# ========== HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    CONN.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,)); CONN.commit()
    kb = [[InlineKeyboardButton("Upgrade with PayPal", url=f"{PAYPAL_SUBSCRIBE_PAGE}?uid={uid}")]]
    await update.message.reply_text(
        "👋 Welcome to *Crypto Alerts Bot!*\n\n"
        "Free plan:\n"
        "• `/price BTC` — live price\n"
        "• `/setalert BTC 30000` — add one alert\n"
        "• `/bulkalerts BTC 30000, ETH 2000, SOL 50` — add many\n"
        "• `/myalerts` — list alerts\n"
        "• `/delalert BTC 30000` — delete one\n"
        "• `/clearalerts` — delete all\n"
        "• `/signals` — 1 demo signal/day\n\n"
        "💎 Premium €7/month:\n"
        "• Multiple alerts (unlimited)\n"
        "• 3 trading setups/day\n"
        "• Weekly recap report\n"
        "• `/premium` — check status\n"
        "• `/coins` — quick list of popular tickers\n",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = sorted(set(k.upper() for k in SYMBOL_TO_ID.keys()))
    rows=[]; line=[]; per_row = 18
    for i,s in enumerate(syms,1):
        line.append(s)
        if i%per_row==0: rows.append(" ".join(line)); line=[]
    if line: rows.append(" ".join(line))
    text = "✅ Popular tickers you can use:\n" + "\n".join(rows) + \
           "\n\nTip: You can also try other symbols — I will auto-detect them."
    await update.message.reply_text(text)

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/price ETH`", parse_mode="Markdown"); return
    coin = context.args[0]
    p = resolve_price_usd(coin)
    if p is not None:
        await update.message.reply_text(f"💰 {coin.upper()} price: ${p}")
    else:
        await update.message.reply_text("❌ Coin not found or API unavailable. Please try again in a moment.")

async def setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/setalert BTC 30000`", parse_mode="Markdown"); return
    coin = normalize_symbol(context.args[0])
    try: target = float(context.args[1])
    except: await update.message.reply_text("❌ Invalid price number."); return

    FREE_LIMIT = 3
    if not is_premium(uid) and len(user_alerts(uid)) >= FREE_LIMIT:
        kb = [[InlineKeyboardButton("Upgrade with PayPal", url=f"{PAYPAL_SUBSCRIBE_PAGE}?uid={uid}")]]
        await update.message.reply_text("Free plan allows up to 3 alerts. Upgrade to Premium for unlimited.",
                                        reply_markup=InlineKeyboardMarkup(kb)); return
    add_alert(uid, coin, target)
    await update.message.reply_text(f"✅ Alert set for {coin.upper()} at ${target:g}")

async def bulkalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pairs = parse_pairs(context.args)
    if not pairs:
        await update.message.reply_text("Usage: `/bulkalerts BTC 30000, ETH 2000, SOL 50`", parse_mode="Markdown"); return
    FREE_LIMIT = 3; current = len(user_alerts(uid)); added=0; skipped=[]
    for coin, target in pairs:
        if not is_premium(uid) and current >= FREE_LIMIT:
            skipped.append((coin, target, "free-limit")); continue
        try:
            add_alert(uid, coin, float(target)); current += 1; added += 1
        except:
            skipped.append((coin, target, "error"))
    msg = f"✅ Added {added} alert(s)."
    if skipped:
        msg += "\n⚠️ Skipped: " + ", ".join([f"{c.upper()} {t:g}" for c,t,_ in skipped])
        if any(tag=="free-limit" for _,_,tag in skipped):
            msg += "\nFree plan allows up to 3 alerts. Upgrade for unlimited."
    await update.message.reply_text(msg)

async def myalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ua = user_alerts(uid)
    if not ua:
        await update.message.reply_text("No alerts set. Try `/setalert BTC 30000`."); return
    lines = [f"• {c.upper()} @ ${t:g}" for c,t in ua]
    await update.message.reply_text("📣 Your alerts:\n" + "\n".join(lines))

async def delalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/delalert BTC 30000`", parse_mode="Markdown"); return
    coin = normalize_symbol(context.args[0])
    try: target = float(context.args[1])
    except: await update.message.reply_text("❌ Invalid price number."); return
    remove_alert(uid, coin, target)
    await update.message.reply_text(f"🗑️ Deleted alert {coin.upper()} @ ${target:g}")

async def clearalerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    CONN.execute("DELETE FROM alerts WHERE user_id=?", (uid,)); CONN.commit()
    await update.message.reply_text("🧹 Cleared all your alerts.")

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_premium(uid):
        text = ("📈 Premium Signals (3/day):\n"
                "• BTC: breakout above $30,200 → tp $31,500\n"
                "• ETH: support near $1,700 → bounce scenario\n"
                "• SOL: momentum watch > $25.4\n\n"
                "Risk disclaimer: Not financial advice.")
        await update.message.reply_text(text)
    else:
        kb = [[InlineKeyboardButton("Upgrade with PayPal", url=f"{PAYPAL_SUBSCRIBE_PAGE}?uid={uid}")]]
        text = ("📈 Demo Signal:\n"
                "• BTC: possible breakout > $30,200\n\n"
                "Unlock 3/day + weekly recap with Premium.")
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    status = "ACTIVE ✅" if is_premium(uid) else "INACTIVE ❌"
    kb = None if is_premium(uid) else [[InlineKeyboardButton("Upgrade with PayPal", url=f"{PAYPAL_SUBSCRIBE_PAGE}?uid={uid}")]]
    await update.message.reply_text(f"💎 Premium status: {status}", reply_markup=InlineKeyboardMarkup(kb) if kb else None)

# ---------- ADMIN ----------
def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _is_admin(uid):
        return
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    total_users = CONN.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active_premium = CONN.execute(
        "SELECT COUNT(*) FROM users WHERE premium_active=1 AND (premium_until IS NULL OR premium_until > ?)",
        (datetime.utcnow().isoformat(),)
    ).fetchone()[0]
    total_alerts = CONN.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    users_with_alerts = CONN.execute("SELECT COUNT(DISTINCT user_id) FROM alerts").fetchone()[0]
    unique_coins = CONN.execute("SELECT COUNT(DISTINCT coin) FROM alerts").fetchone()[0]

    msg = (
        f"🛠️ Admin Stats ({now})\n"
        f"• Users: {total_users}\n"
        f"• Premium ACTIVE: {active_premium}\n"
        f"• Alerts total: {total_alerts}\n"
        f"• Users with alerts: {users_with_alerts}\n"
        f"• Unique coins: {unique_coins}\n"
        f"• Check interval: {CHECK_INTERVAL_SEC}s"
    )
    await update.message.reply_text(msg)

# ========== PRICE FETCH & ALERTS ==========
def fetch_prices(coins):
    """Return dict keyed by CoinGecko IDs with USD prices using resolver + cache."""
    out = {}
    for c in coins:
        cg_id = to_cg_id(c)
        p = resolve_price_usd(c)
        if p is not None:
            out[cg_id] = {"usd": p}
    return out

async def check_alerts_once(context):
    app = context.application
    try:
        coins = list_unique_coins()
        prices = fetch_prices(coins)
        cur = CONN.execute("SELECT user_id, coin, target FROM alerts")
        to_remove = []
        for uid, coin, target in cur.fetchall():
            key = to_cg_id(coin)
            if key in prices and "usd" in prices[key]:
                current = prices[key]["usd"]
                if current >= target:
                    try:
                        await app.bot.send_message(uid, f"🚨 {coin.upper()} hit ${current:g} (target ${target:g})")
                        to_remove.append((uid, coin, target))
                    except Exception as e:
                        logging.warning(f"send fail: {e}")
        for uid, coin, target in to_remove:
            remove_alert(uid, coin, target)
    except Exception as e:
        logging.error(f"alerts check err: {e}")

# ========== BOOT ==========
def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("coins", coins))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("setalert", setalert))
    app.add_handler(CommandHandler("bulkalerts", bulkalerts))
    app.add_handler(CommandHandler("myalerts", myalerts))
    app.add_handler(CommandHandler("delalert", delalert))
    app.add_handler(CommandHandler("clearalerts", clearalerts))
    app.add_handler(CommandHandler("signals", signals))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("adminstats", adminstats))

    # Ensure JobQueue exists (defensive) & schedule alert checks
    jq = app.job_queue
    if jq is None:
        jq = JobQueue(); jq.set_application(app); jq.start()
    jq.run_repeating(check_alerts_once, interval=CHECK_INTERVAL_SEC, first=5)

    logging.info("🤖 Bot running (polling)…")
    app.run_polling()

if __name__ == "__main__":
    run_bot()
