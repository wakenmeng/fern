-- Fern schema. Money is bigint cents. Every row that belongs to a demo space carries space_id,
-- and the application filters on it. See docs/DESIGN.md §4–§5.

CREATE TABLE spaces (
    id          uuid PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE customers (
    id            uuid PRIMARY KEY,
    space_id      uuid NOT NULL REFERENCES spaces (id),
    kind          text NOT NULL CHECK (kind IN ('primary', 'npc')),
    display_name  text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Ledger accounts. raw_sum_minor is the signed sum of postings (debit +, credit -).
CREATE TABLE accounts (
    id             uuid PRIMARY KEY,
    space_id       uuid NOT NULL REFERENCES spaces (id),
    role           text NOT NULL,
    customer_id    uuid REFERENCES customers (id),
    raw_sum_minor  bigint NOT NULL DEFAULT 0,
    UNIQUE (space_id, role, customer_id)
);
CREATE UNIQUE INDEX accounts_system_role_uq ON accounts (space_id, role) WHERE customer_id IS NULL;

-- Transactions as they posted at the bank. amount_minor > 0: money into our account.
CREATE TABLE bank_transactions (
    id                   uuid PRIMARY KEY,
    space_id             uuid NOT NULL REFERENCES spaces (id),
    provider             text NOT NULL,
    provider_txn_id      text NOT NULL,
    amount_minor         bigint NOT NULL CHECK (amount_minor <> 0),
    kind                 text NOT NULL,
    posted_at            timestamptz NOT NULL,
    UNIQUE (provider, provider_txn_id)
);

CREATE TABLE transfers (
    id                 uuid PRIMARY KEY,
    space_id           uuid NOT NULL REFERENCES spaces (id),
    kind               text NOT NULL CHECK (kind IN ('payroll', 'pull', 'p2p', 'withdrawal')),
    status             text NOT NULL,
    amount_minor       bigint NOT NULL CHECK (amount_minor > 0),
    from_customer_id   uuid REFERENCES customers (id),
    to_customer_id     uuid REFERENCES customers (id),
    counterparty_name  text,
    bank_transfer_id   text UNIQUE,
    return_code        text,
    failure_reason     text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE transfer_events (
    id           bigserial PRIMARY KEY,
    transfer_id  uuid NOT NULL REFERENCES transfers (id),
    event        text NOT NULL,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX transfer_events_transfer_idx ON transfer_events (transfer_id, id);

-- One journal = one accounting effect with exactly two postings.
CREATE TABLE journals (
    id                   uuid PRIMARY KEY,
    space_id             uuid NOT NULL REFERENCES spaces (id),
    effect               text NOT NULL,
    amount_minor         bigint NOT NULL CHECK (amount_minor > 0),
    debit_account_id     uuid NOT NULL REFERENCES accounts (id),
    credit_account_id    uuid NOT NULL REFERENCES accounts (id),
    transfer_id          uuid REFERENCES transfers (id),
    bank_transaction_id  uuid UNIQUE REFERENCES bank_transactions (id),
    dedupe_key           text NOT NULL UNIQUE,
    created_at           timestamptz NOT NULL DEFAULT now(),
    CHECK (debit_account_id <> credit_account_id)
);

CREATE TABLE postings (
    id            bigserial PRIMARY KEY,
    journal_id    uuid NOT NULL REFERENCES journals (id),
    account_id    uuid NOT NULL REFERENCES accounts (id),
    amount_minor  bigint NOT NULL CHECK (amount_minor <> 0)
);
CREATE INDEX postings_account_idx ON postings (account_id);

-- Money reserved for a withdrawal until the bank accepts it.
CREATE TABLE holds (
    transfer_id   uuid PRIMARY KEY REFERENCES transfers (id),
    customer_id   uuid NOT NULL REFERENCES customers (id),
    amount_minor  bigint NOT NULL CHECK (amount_minor > 0),
    status        text NOT NULL CHECK (status IN ('active', 'consumed', 'released'))
);

-- Pulled-in money that is not yet available to spend.
CREATE TABLE restrictions (
    transfer_id   uuid PRIMARY KEY REFERENCES transfers (id),
    customer_id   uuid NOT NULL REFERENCES customers (id),
    amount_minor  bigint NOT NULL CHECK (amount_minor > 0),
    release_at    timestamptz,
    status        text NOT NULL CHECK (status IN ('active', 'released', 'terminated'))
);

CREATE TABLE idempotency_requests (
    space_id         uuid NOT NULL REFERENCES spaces (id),
    principal        text NOT NULL,
    operation        text NOT NULL,
    idem_key         text NOT NULL,
    request_hash     text NOT NULL,
    status           text NOT NULL CHECK (status IN ('in_progress', 'completed')),
    response_status  integer,
    response_body    jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (space_id, principal, operation, idem_key)
);

-- Webhook inbox: one row per bank event id.
CREATE TABLE bank_events (
    provider     text NOT NULL,
    event_id     text NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, event_id)
);

-- S5: writing off a negative balance needs a second person to approve.
CREATE TABLE write_offs (
    id            uuid PRIMARY KEY,
    space_id      uuid NOT NULL REFERENCES spaces (id),
    customer_id   uuid NOT NULL REFERENCES customers (id),
    amount_minor  bigint NOT NULL CHECK (amount_minor > 0),
    status        text NOT NULL CHECK (status IN ('pending', 'approved')),
    requested_by  text NOT NULL,
    approved_by   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (approved_by IS NULL OR approved_by <> requested_by)
);
