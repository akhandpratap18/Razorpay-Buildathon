-- ============================================================
-- Migration 008: Add B2B category
-- ============================================================

ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_category_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_category_check CHECK (category IN (
    'bank_downtime', 'card_limit_exceeded', 'abandoned_cart',
    'fraud_hard_stop', 'unknown', 'subscription_charge_failed',
    'b2b_promise_to_pay'
));
