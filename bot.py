#!/usr/bin/env python3
import logging, os, sqlite3
from datetime import datetime, timedelta
import requests

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
    "xlq":"liquidlayer","one":"harmony","neo":"neo","ksm":"kusama","xdc":"xdce-crowd-sale","icx":"icon",
    # DeFi & DEX
    "uni":"uniswap","aave":"aave","comp":"compound-governance-token","snx":"synthetix-network-token",
    "crv":"curve-dao-token","sushi":"sushi","yfi":"yearn-finance","bal":"balancer","cake":"pancakeswap-token",
    "1inch":"1inch","cvx":"convex-finance","gmx":"gmx","dydx":"dydx","joe":"joe","kswap":"kyber-network-crystal",
    "blur":"blur","pendle":"pendle","rndr":"render-token",
    # NFT/Gaming/Metaverse
    "sand":"the-sandbox","mana":"decentraland","axs":"axie-infinity","enj":"enjincoin","ape":"apecoin",
    "gmt":"stepn","imx":"immutable-x","beam":"beam-2","ron":"ronin",
    # Oracles, infra, AI
    "ocean":"ocean-protocol","fet":"fetch-ai","agix":"singularitynet","tao":"bittensor","phb":"phoenix-global",
    "band":"band-protocol","api3":"api3",
    # Payments & others
    "xvg":"verge","dash":"dash","zec":"zcash","xdc":"xdce-crowd-sale","kas":"kaspa","wif":"dogwifcoin",
    "bonk":"bonk","pepe":"pepe","shib":"shiba-inu","floki":"floki","safemoon":"safemoon",
    "qnt":"quant-network","hnt":"helium","sfp":"safepal","rox":"retreeb",
    # Stablecoins (reference)
    "usdt":"tether","usdc":"usd-coin","dai":"dai","tusd":"true-usd","usdd":"usdd","frax":"frax",
    # Exchanges/CEXs
    "okb":"okb","gt":"gatechain-token","leo":"leo-token","cro":"crypto-com-chain",
    # More popular requests
    "arb":"arbitrum","op":"optimism","sui":"sui","apt":"aptos","kas":"kaspa","sei":"sei-network","ton":"the-open-network",
    "wbtc":"wrapped-bitcoin","weeth":"wrapped-eeth","steth":"staked-ether","reth":"rocket-pool-eth","wbnb":"wbnb",
    "opnx":"opnx","wld":"worldcoin-wld","pyth":"pyth-network","tomi":"tominet","core":"coredaoorg","ena":"ethena",
    "aevo":"aevo","alt":"altlayer","sfrxeth":"staked-frax-ether","fxs":"frax-share","mpl":"maple",
}
_SYMBOL_CACHE = {}  # auto-filled via search

def _search_coingecko_id(symbol: str) -> str | None:
    """Query CoinGecko search API to resolve a symbol to an ID."""
    try:
        r = requests.get(COINGECKO_SEARCH, params={"query": symbol}, timeout=8)
        data = r.json()
        # Prefer exact symbol match
        for c in data.get("coins", []):
            if c.get("symbol","").lower() == symbol.lower():
                return c["id"]
        # Fallback: first hit
        if data.get("coins"):
            return data["coins"][0]["id"]
    except Exception:
        return None
    return None

def to_cg_id(symbol_or_id: str) -> str:
    """
    Return CoinGecko ID for a given ticker. Priority:
    1) extended static map, 2) runtime cache, 3) CoinGecko search, 4) fallback to input.
    """
    s = symbol_or_id.strip().lower()
    if s in SYMBOL_TO_ID:
        return SYMBOL_TO_ID[s]
    if s in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[s]
    # If it already looks like an id (has '-'), accept
    if "-" in s or " " in s:
        return s
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
    """Accepts: /bulkalerts BTC 30000, ETH 2000, SOL 50  (or space-separated)"""
    if not text_args: return []
    raw = text_args[0] if len(text_args)==1 else " ".join(text_args)
    raw = raw.replace(";", " ").replace(",", " ")
    toks = raw.split()
    out=[]; i=0
    while i < len(toks)-1:
        coin=toks[i].strip().lower(); price_str=toks[i+1].strip().rstrip(",;")
        try:
            out.append((coin, float(price_str))); i+=2
        except: i+=1
    return out

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
    # format in rows of 16 for readability
    rows=[]; line=[]
    for i,s in enumerate(syms,1):
        line.append(s)
        if i%16==0: rows.append(" ".join(line)); line=[]
    if line: rows.append(" ".join(line))
    text = "✅ Popular tickers you can use:\n" + "\n".join(rows) + \
           "\n\nTip: You can also try other symbols — I will auto-detect them."
    await update.message.reply_text(text)

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/price BTC`", parse_mode="Markdown"); return
    coin = context.args[0].lower()
    cg_id = to_cg_id(coin)
    try:
        r = requests.get(COINGECKO_SIMPLE, params={"ids": cg_id, "vs_currencies": "usd"}, timeout=10)
        data = r.json()
        if cg_id not in data:
            await update.message.reply_text("❌ Coin not found."); return
        p = data[cg_id]["usd"]
        await update.message.reply_text(f"💰 {coin.upper()} price: ${p}")
    except Exception:
        await update.message.reply_text("Error fetching price.")

async def setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/setalert BTC 30000`", parse_mode="Markdown"); return
    coin = context.args[0].lower()
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
    coin = context.args[0].lower()
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

# ========== PRICE FETCH & ALERTS ==========
def fetch_prices(coins):
    """Return dict keyed by CoinGecko IDs with USD prices."""
    if not coins: return {}
    ids = [to_cg_id(c) for c in coins]
    try:
        r = requests.get(COINGECKO_SIMPLE, params={"ids": ",".join(ids), "vs_currencies": "usd"}, timeout=10)
        return r.json() or {}
    except Exception:
        return {}

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

    # Ensure JobQueue exists (defensive) & schedule alert checks
    jq = app.job_queue
    if jq is None:
        jq = JobQueue(); jq.set_application(app); jq.start()
    jq.run_repeating(check_alerts_once, interval=CHECK_INTERVAL_SEC, first=5)

    logging.info("🤖 Bot running (polling)…")
    app.run_polling()

if __name__ == "__main__":
    run_bot()
