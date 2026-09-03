CREATE TABLE IF NOT EXISTS chat_history (
    recovery_token TEXT PRIMARY KEY,
    messages JSONB NOT NULL DEFAULT '[]'::jsonb
);
