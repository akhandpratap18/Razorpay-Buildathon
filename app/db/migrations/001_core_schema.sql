-- ============================================================
-- Migration 001: Core Schema
-- Recoup — Supabase Postgres
-- ============================================================
-- Run this in: Supabase Dashboard → SQL Editor → New Query
-- ============================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Transactions ──────────────────────────────────────────────────────────────
-- One row per unique payment failure event. 
-- event_id is the idempotency key (prevents duplicate processing).
CREATE TABLE IF NOT EXISTS transactions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id        TEXT        UNIQUE NOT NULL,     -- idempotency key
    payment_id      TEXT        NOT NULL,
    order_id        TEXT,
    amount_inr      DECIMAL(12, 2) NOT NULL CHECK (amount_inr > 0),
    currency        TEXT        NOT NULL DEFAULT 'INR',
    method          TEXT,
    error_code      TEXT,
    error_description TEXT,
    error_source    TEXT,
    error_reason    TEXT,
    contact         TEXT,       -- masked in logs; full value only via audit path
    email           TEXT,       -- masked in logs
    -- Classification
    category        TEXT        CHECK (category IN (
                                    'bank_downtime',
                                    'card_limit_exceeded',
                                    'abandoned_cart',
                                    'fraud_hard_stop',
                                    'unknown',
                                    'subscription_charge_failed',
                                    'b2b_promise_to_pay'
                                )),
    risk_level      TEXT        CHECK (risk_level IN ('low', 'medium', 'high')),
    classified_by   TEXT,       -- 'rules' | 'llm'
    -- Recovery state
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (status IN (
                                    'pending',
                                    'processing',
                                    'recovered',
                                    'failed',
                                    'escalated',
                                    'killed'
                                )),
    recovery_attempts INTEGER   NOT NULL DEFAULT 0,
    recovery_link_url TEXT,
    recovery_link_id  TEXT,
    -- Timestamps
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions (status);
CREATE INDEX IF NOT EXISTS idx_transactions_created ON transactions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_order ON transactions (order_id);

-- Auto-update updated_at on any row change
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_transactions_updated_at
    BEFORE UPDATE ON transactions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
