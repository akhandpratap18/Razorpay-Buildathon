-- ============================================================
-- Migration 002: Job Queue
-- Recoup — Postgres-native queue (no Redis needed)
-- ============================================================
-- Uses SELECT ... FOR UPDATE SKIP LOCKED for concurrent worker safety.
-- Any number of worker processes can poll simultaneously with zero collisions.
-- ============================================================

CREATE TABLE IF NOT EXISTS job_queue (
    id              BIGSERIAL   PRIMARY KEY,
    event_id        TEXT        UNIQUE NOT NULL,     -- idempotency: one job per event
    transaction_id  UUID        REFERENCES transactions(id) ON DELETE CASCADE,
    job_type        TEXT        NOT NULL,            -- 'payment.failed' | 'order.paid' etc.
    payload         JSONB       NOT NULL DEFAULT '{}',
    -- State machine
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (status IN (
                                    'pending',
                                    'claimed',
                                    'done',
                                    'failed',
                                    'dead_letter'
                                )),
    attempts        INTEGER     NOT NULL DEFAULT 0,
    max_attempts    INTEGER     NOT NULL DEFAULT 3,
    -- Scheduling
    run_after       TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- used for backoff delays
    -- Worker tracking
    claimed_at      TIMESTAMPTZ,
    worker_id       TEXT,
    -- Result
    result          JSONB,
    error_message   TEXT,
    -- Timestamps
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Critical: this index makes SKIP LOCKED polls fast
CREATE INDEX IF NOT EXISTS idx_job_queue_claimable
    ON job_queue (run_after, status)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_job_queue_transaction
    ON job_queue (transaction_id);

CREATE OR REPLACE TRIGGER trg_job_queue_updated_at
    BEFORE UPDATE ON job_queue
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Convenience view: pending jobs ready to run ───────────────────────────────
CREATE OR REPLACE VIEW v_pending_jobs AS
SELECT *
FROM job_queue
WHERE status = 'pending'
  AND run_after <= NOW()
ORDER BY run_after;
