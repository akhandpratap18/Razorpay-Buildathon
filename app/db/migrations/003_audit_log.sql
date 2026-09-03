-- ============================================================
-- Migration 003: Audit Log
-- Recoup — Hash-chained tamper-evident audit trail
-- ============================================================
-- Every state transition and action is logged here.
-- row_hash = sha256(prev_hash || row_json) makes the chain
-- tamper-evident: any modification breaks all subsequent hashes.
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL   PRIMARY KEY,
    transaction_id  UUID        REFERENCES transactions(id) ON DELETE CASCADE,
    event_type      TEXT        NOT NULL,    -- e.g. 'classified', 'route_selected', 'link_sent'
    actor           TEXT        NOT NULL,    -- graph node name, e.g. 'diagnostic_agent'
    payload         JSONB       NOT NULL,    -- full event data (PII masked)
    prev_hash       TEXT,                    -- hash of previous row (NULL for first row)
    row_hash        TEXT        NOT NULL,    -- sha256(prev_hash || payload::text)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_transaction ON audit_log (transaction_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_type  ON audit_log (event_type);

-- ── Opt-outs (honored permanently, checked before every outbound message) ────
CREATE TABLE IF NOT EXISTS opt_outs (
    phone           TEXT        PRIMARY KEY,
    opted_out_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    channel         TEXT,                    -- 'whatsapp' | 'sms' | 'any'
    transaction_id  UUID        REFERENCES transactions(id)
);

-- ── Communication log ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS communication_log (
    id              BIGSERIAL   PRIMARY KEY,
    transaction_id  UUID        REFERENCES transactions(id) ON DELETE CASCADE,
    channel         TEXT        NOT NULL CHECK (channel IN ('whatsapp', 'sms', 'email')),
    direction       TEXT        NOT NULL CHECK (direction IN ('outbound', 'inbound')),
    status          TEXT        NOT NULL DEFAULT 'queued'
                                CHECK (status IN ('queued', 'sent', 'delivered', 'read', 'failed')),
    provider_msg_id TEXT,
    message_preview TEXT,                    -- first 100 chars only, no full PII
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE TRIGGER trg_comms_updated_at
    BEFORE UPDATE ON communication_log
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
