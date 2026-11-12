-- Reconciliation (DESIGN §7) and AI ops reports (DESIGN §9).

CREATE TABLE recon_runs (
    id             uuid PRIMARY KEY,
    space_id       uuid NOT NULL REFERENCES spaces (id),
    started_at     timestamptz NOT NULL DEFAULT now(),
    provider       text NOT NULL,
    bank_balance   bigint,          -- what the bank says (null when the bank can't tell us)
    book_balance   bigint NOT NULL, -- our "bank" account in the ledger
    scanned        int NOT NULL,
    matched        int NOT NULL,
    open_exceptions int NOT NULL
);
CREATE INDEX recon_runs_space_recent ON recon_runs (space_id, started_at DESC);

-- One row per discrepancy, kept across runs: a later run resolves it or leaves it open.
CREATE TABLE recon_exceptions (
    id              uuid PRIMARY KEY,
    space_id        uuid NOT NULL REFERENCES spaces (id),
    kind            text NOT NULL CHECK (kind IN (
                        'missing_in_books',   -- at the bank, not in our ledger
                        'missing_at_bank',    -- in our ledger, the bank has no such movement
                        'amount_mismatch',
                        'in_flight_unconfirmed', -- the bank moved money for a transfer still "unknown" to us
                        'balance_mismatch')),
    provider_txn_id text NOT NULL,
    amount_minor    bigint NOT NULL,
    detail          text NOT NULL,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'escalated')),
    first_run_id    uuid NOT NULL REFERENCES recon_runs (id),
    last_run_id     uuid NOT NULL REFERENCES recon_runs (id),
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    UNIQUE (space_id, kind, provider_txn_id)
);

-- Bank-side movements a drill created on purpose, outside the ledger. The simulator has no
-- per-account listing, so reconciliation reads these to see what "the bank" did.
CREATE TABLE drill_bank_transfers (
    bank_transfer_id text PRIMARY KEY,
    space_id         uuid NOT NULL REFERENCES spaces (id),
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ai_reports (
    id              uuid PRIMARY KEY,
    space_id        uuid NOT NULL REFERENCES spaces (id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    status          text NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    source          text NOT NULL CHECK (source IN ('claude', 'rules')),
    model           text,
    -- Budget: reserved up front at the worst-case price, replaced by the actual cost when done.
    reserved_micros bigint NOT NULL DEFAULT 0,
    cost_micros     bigint,
    tokens_in       int,
    tokens_out      int,
    content         jsonb,
    error           text
);
CREATE INDEX ai_reports_space_recent ON ai_reports (space_id, created_at DESC);
CREATE INDEX ai_reports_day ON ai_reports (created_at);
