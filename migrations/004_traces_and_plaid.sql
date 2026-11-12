-- Request traces shown next to the demo app (FRONTEND §3). Only state-changing requests are kept,
-- and only for the space that made them.
CREATE TABLE traces (
    id          text PRIMARY KEY,
    space_id    uuid NOT NULL REFERENCES spaces (id),
    name        text NOT NULL,
    status      text NOT NULL CHECK (status IN ('ok', 'error')),
    duration_us bigint NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    spans       jsonb NOT NULL
);
CREATE INDEX traces_space_recent ON traces (space_id, started_at DESC);

-- The space's linked external account: a Plaid sandbox Item, created on the first pull.
ALTER TABLE spaces ADD COLUMN plaid_access_token text;
