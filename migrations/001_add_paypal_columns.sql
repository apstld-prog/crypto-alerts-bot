
-- migrations/001_add_paypal_columns.sql
-- Safe, rerunnable migration για πίνακα subscriptions (PayPal support)
-- Δημιουργεί τον πίνακα αν δεν υπάρχει και προσθέτει/διορθώνει στήλες με IF NOT EXISTS.

BEGIN;

-- 1) Δημιούργησε τον πίνακα subscriptions αν δεν υπάρχει
CREATE TABLE IF NOT EXISTS subscriptions (
    id                   SERIAL PRIMARY KEY,
    user_id              INTEGER,                        -- θα συνδεθεί με users.id αν υπάρχει ο πίνακας
    provider             VARCHAR(32) NOT NULL DEFAULT 'paypal',
    provider_status      VARCHAR(64) NOT NULL DEFAULT 'UNKNOWN',       -- π.χ. ACTIVE, CANCELLED, EXPIRED (όπως έρχεται από provider)
    status_internal      VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',       -- δική μας κατάσταση: ACTIVE | CANCEL_AT_PERIOD_END | CANCELLED | UNKNOWN
    provider_ref         VARCHAR(128),                                  -- π.χ. billing_agreement_id / subscription_id
    current_period_end   TIMESTAMPTZ,                                   -- μέχρι πότε ισχύει η περίοδος
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2) Στήλες που μπορεί να λείπουν (ασφαλές να τρέξει ξανά)
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS provider VARCHAR(32) NOT NULL DEFAULT 'paypal';
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS provider_status VARCHAR(64) NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS status_internal VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS provider_ref VARCHAR(128);
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMPTZ;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS user_id INTEGER;

-- 3) Indexes για γρήγορα admin queries
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions (user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status_internal ON subscriptions (status_internal);
CREATE INDEX IF NOT EXISTS idx_subscriptions_current_period_end ON subscriptions (current_period_end);

-- 4) (Προαιρετικό) Πρόσθεσε foreign key προς users.id ΜΟΝΟ αν υπάρχει ο πίνακας users
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'users'
    ) THEN
        -- Αν δεν υπάρχει ήδη το constraint, πρόσθεσέ το
        IF NOT EXISTS (
            SELECT 1
            FROM information_schema.table_constraints
            WHERE table_schema = 'public'
              AND table_name = 'subscriptions'
              AND constraint_name = 'fk_subscriptions_user'
        ) THEN
            ALTER TABLE subscriptions
            ADD CONSTRAINT fk_subscriptions_user
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE SET NULL;
        END IF;
    END IF;
END$$;

COMMIT;
