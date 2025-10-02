# CryptoAlerts77 — FULL SERVER BUNDLE (Ready-to-Copy)

Περιέχει:
- `server_combined.py` — κύριο FastAPI app
- `api_embed.py` — /api/* (alerts, plan, market/news, SDUI app-config, billing stub)
- `api_linking_embed.py` — /api/link/* (PIN/QR linking + FCM token save)
- `push_notify.py` — helper για FCM push από τον worker
- `requirements.txt` — όλες οι εξαρτήσεις
- `start.sh` — έτοιμο start script

## Βήματα
1) Αντέγραψε **όλα** τα αρχεία στο root του server project σου.
2) Render → Environment:
   - `DATABASE_URL`  = Postgres URL σου
   - `API_KEY`       = μυστικό (π.χ. `ca77_prod_abc123`)
   - `ADMIN_TELEGRAM_IDS` = π.χ. `123456789`
   - (για push) `GOOGLE_APPLICATION_CREDENTIALS` = path του service-account.json
3) Start command στο Render: `/bin/bash -lc './start.sh'`
4) Έλεγχος:
   - `GET /` → ok
   - `GET /api/app-config` με header `X-API-Key`
   - `POST /api/link/start` → παίρνεις PIN
   - Στο bot πρόσθεσε `/link` (δες README linking) και κάλεσε `POST /api/link/confirm`
   - Προαιρετικά: βάλε `push_notify.send_push_to_tg(...)` στον worker

## Σημειώσεις
- Το API είναι συμβατό με την υπάρχουσα DB/λογική του bot σου.
- Το SDUI `/api/app-config` σου επιτρέπει να αλλάζεις tabs/sections **χωρίς νέο build**.
- Το billing endpoint είναι **stub** — για πραγματικό verify με Play θα δοθεί ξεχωριστό module.
