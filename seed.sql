-- Run: psql "$DATABASE_URL" -f seed.sql

INSERT INTO users (id, telegram_id, is_premium, created_at)
VALUES (1, '123456789', FALSE, NOW())
ON CONFLICT DO NOTHING;

INSERT INTO alerts (id, user_id, enabled, symbol, rule, value, cooldown_seconds, last_fired_at, user_seq, created_at)
VALUES (1, 1, TRUE, 'BTCUSDT', 'price_above', 30000, 900, NULL, 1, NOW())
ON CONFLICT DO NOTHING;
