# Crypto Alerts Telegram Bot (EUR / €7 per month)

English-only bot with:
- `/price <coin>` (CoinGecko)
- `/setalert <coin> <price>` (free: up to 3 alerts; premium: unlimited)
- `/bulkalerts BTC 30000, ETH 2000, SOL 50` (add many at once)
- `/myalerts`
- `/delalert <coin> <price>`
- `/clearalerts`
- `/signals` (demo vs premium 3/day)
- `/premium` (status)
- PayPal **Subscriptions** (EUR) with Webhook → auto-activate Premium

## 1) Configure PayPal (account: `inform_product@yahoo.gr`)
1. Log in to PayPal Developer Dashboard.
2. Create a REST API App → get Client ID.
3. Create Product: "Crypto Alerts Premium".
4. Create Plan: **€7/month** in **EUR** → copy Plan ID (e.g. `P-XXXX`).
5. Add a Webhook pointing to `https://<your-domain>/paypal/webhook` with events:
   - `BILLING.SUBSCRIPTION.ACTIVATED`
   - `BILLING.SUBSCRIPTION.RE-ACTIVATED`
   - `PAYMENT.SALE.COMPLETED` (and/or `PAYMENT.CAPTURE.COMPLETED`)
   - (optional) `BILLING.SUBSCRIPTION.CANCELLED`

## 2) Fill placeholders
- In `subscribe.html`:
  - Replace `YOUR_PAYPAL_CLIENT_ID` with your PayPal Client ID.
  - Replace `P-REPLACE-WITH-YOUR-EUR-PLAN-ID` with your EUR Plan ID (€7/month).
- Environment variables:
  - `BOT_TOKEN` = token from @BotFather
  - `PAYPAL_SUBSCRIBE_PAGE` = `https://<your-domain>/subscribe.html`

## 3) Deploy (Render/Heroku/VPS)
- `pip install -r requirements.txt`
- Start: `python server_combined.py`
- The service launches:
  - Flask webhook server at `/paypal/webhook` and `/subscribe.html`
  - Telegram bot (polling)

## 4) Test flow
- In Telegram: `/start`, `/price BTC`, `/setalert BTC 30000`, `/bulkalerts BTC 30000, ETH 2000`, `/myalerts`, `/signals`, `/premium`.
- Subscribe from the Upgrade button → PayPal page → approve.
- Webhook sets Premium active (31 days). Check `/premium`.

## Notes
- For production, implement PayPal **Webhook signature verification** and set `VERIFY_WEBHOOK=true`.
- SQLite is used for simplicity; switch to Postgres/Firestore for scale.
- Alerts check every 60s; adjust `CHECK_INTERVAL_SEC` if needed.
- Prices via CoinGecko; switch to Binance API for realtime if desired.
