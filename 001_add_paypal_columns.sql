
-- Add provider_ref and helpful indexes if not exists
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS provider_ref VARCHAR(128);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_provider_ref ON subscriptions(provider_ref);
CREATE INDEX IF NOT EXISTS idx_subscriptions_current_period_end ON subscriptions(current_period_end);
