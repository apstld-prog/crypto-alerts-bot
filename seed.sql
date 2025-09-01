-- Demo seed data for Crypto Alerts
-- Run: psql "$DATABASE_URL" -f seed.sql

-- Demo user
INSERT INTO users (id, telegram_id, is_premium, created_at)
VALUES (1, '123456789', FALSE, NOW())
ON CONFLICT DO NOTHING;

-- Demo alert
INSERT INTO alerts (id, user_id, enabled, symbol, rule, value, cooldown_seconds, last_fired_at, expires_at, created_at)
VALUES (1, 1, TRUE, 'BTCUSDT', 'price_above', 30000, 900, NULL, NULL, NOW())
ON CONFLICT DO NOTHING;

-- Demo subscription (ACTIVE)
INSERT INTO subscriptions (id, user_id, provider, provider_status, status_internal, current_period_end, created_at)
VALUES (1, 1, 'stripe', 'active', 'ACTIVE', NOW() + INTERVAL '30 days', NOW())
ON CONFLICT DO NOTHING;
