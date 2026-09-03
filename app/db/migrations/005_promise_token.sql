-- Migration 005: Add recovery token to promise_to_pay

ALTER TABLE promise_to_pay ADD COLUMN IF NOT EXISTS recovery_token TEXT UNIQUE;

-- We don't have a built-in random string generator in pure postgres without extensions,
-- so we'll just use a UUID cast to text for the backfill
UPDATE promise_to_pay 
SET recovery_token = gen_random_uuid()::text 
WHERE recovery_token IS NULL;
