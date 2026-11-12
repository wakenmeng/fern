-- Pre-funded account pool (DESIGN §2.1). A pooled space is opened and its payroll lands at the
-- bank before any visitor arrives; spaces created on demand start out claimed.
ALTER TABLE spaces
    ADD COLUMN pool_status text NOT NULL DEFAULT 'claimed'
        CHECK (pool_status IN ('provisioning', 'ready', 'claimed')),
    ADD COLUMN claimed_at timestamptz,
    ADD COLUMN payroll_requested_at timestamptz,
    -- The incoming credit already posted on the space's bank account, waiting for S1 to record it.
    ADD COLUMN payroll_txn_id text,
    ADD COLUMN payroll_amount_minor bigint,
    ADD COLUMN payroll_posted_at timestamptz;

CREATE INDEX spaces_ready ON spaces (created_at) WHERE pool_status = 'ready';
