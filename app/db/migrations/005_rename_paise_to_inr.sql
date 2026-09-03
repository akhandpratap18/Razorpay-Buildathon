-- Migration 005: Convert paise to INR

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'transactions' AND column_name = 'amount_inr'
    ) THEN
        ALTER TABLE transactions ADD COLUMN amount_inr DECIMAL(12, 2);
        UPDATE transactions SET amount_inr = CAST(amount_paise AS DECIMAL(12, 2)) / 100.0;
        ALTER TABLE transactions ALTER COLUMN amount_inr SET NOT NULL;
        ALTER TABLE transactions DROP COLUMN amount_paise;
    END IF;
END $$;
