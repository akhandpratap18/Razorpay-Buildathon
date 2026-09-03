-- Migration 006: Add subscription_charge_failed to category

ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_category_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_category_check CHECK (category IN (
    'bank_downtime',
    'card_limit_exceeded',
    'abandoned_cart',
    'fraud_hard_stop',
    'unknown',
    'subscription_charge_failed'
));
