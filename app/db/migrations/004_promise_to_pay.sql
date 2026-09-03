-- Migration 004: Promise to Pay Table

CREATE TABLE IF NOT EXISTS promise_to_pay (
  id BIGSERIAL PRIMARY KEY,
  transaction_id TEXT NOT NULL,
  original_order_id TEXT NOT NULL,
  immediate_leg_inr DECIMAL(12, 2) NOT NULL,
  promised_leg_inr DECIMAL(12, 2) NOT NULL,
  due_date DATE NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  reminder_count INT NOT NULL DEFAULT 0,
  max_reminders INT NOT NULL DEFAULT 3,
  promised_payment_link TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  confirmed_at TIMESTAMPTZ
);

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS recovery_token TEXT UNIQUE;
