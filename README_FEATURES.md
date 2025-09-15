
# Crypto Alerts Bot — Extra Features Pack (v1)
All code is in **English**. Below are the new features and how to enable them.

## What's Included
- `features_market.py`: Market helpers (Binance prices, gainers/losers, funding, klines, Fear & Greed, news).
- `models_extras.py`: Extra SQLAlchemy models (`UserSettings`) with `init_extras()` to create tables.
- `migrate_user_settings.py`: Idempotent migration to create `user_settings` table.
- `commands_extra.py`: Telegram command handlers you can mount in your bot.
- `worker_extra.py`: Optional background scanner for Pump & Dump alerts.

## New Commands (bot-side)
- `/feargreed` → Show current Fear & Greed Index.
- `/funding [SYMBOL]` → Show last funding rate (Binance futures). If no symbol: returns 10 extremes by |funding|.
- `/topgainers` and `/toplosers` → 24h % change (Binance spot).
- `/chart <SYMBOL>` → Quick mini chart (24h close) rendered via QuickChart.
- `/news [N]` → Latest crypto headlines (default N=5).
- `/dca <amount_per_buy> <buys> <symbol>` → Simple DCA schedule calculator (uses current price for estimate).
- `/pumplive on|off [threshold%]` → Opt-in/out Pump & Dump push alerts (5m change; default 10%).
- `/whale [min_usd]` → Recent "whale" transactions via Whale Alert API (requires `WHALE_ALERT_API_KEY`).

## Environment Variables (optional)
- `WHALE_ALERT_API_KEY` → For `/whale` (https://docs.whale-alert.io/). If not set, command will explain how to enable.
- `NEWS_FEEDS` → Comma-separated RSS URLs. Defaults to CoinDesk + CoinTelegraph.
- `PUMP_THRESHOLD_PERCENT` → Default pump threshold for `worker_extra.py` (e.g., `10`). Users can override per chat via `/pumplive`.
- `SYMBOLS_SCAN` → Comma-separated symbols (e.g., `BTCUSDT,ETHUSDT,SOLUSDT`) to scan for pump alerts. If empty, a small default set is used.

## How to Integrate
1) **Run migration** (once):
```
python migrate_user_settings.py
```
2) **Update your bot** (`daemon.py`):
```python
# Add near the top:
from commands_extra import register_extra_handlers
from models_extras import init_extras
from worker_extra import start_pump_watcher, stop_pump_watcher

# After init_db():
init_extras()

# After you build the Application and add your existing handlers:
register_extra_handlers(app)  # mounts /feargreed, /funding, /topgainers, /toplosers, /chart, /news, /dca, /pumplive, /whale

# Optional: start the background pump watcher when RUN_ALERTS is enabled
if os.getenv("RUN_ALERTS", "1") == "1":
    start_pump_watcher()
```

3) **Deploy** and test in Telegram.
- Try: `/feargreed`, `/topgainers`, `/funding BTC`, `/chart BTC`, `/news`, `/dca 20 12 BTC`, `/pumplive on 12`, `/whale 500000`.

> Note: This pack avoids DB changes except a tiny `user_settings` table for user opt-ins.
