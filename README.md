# Fern

A neobank backend demo: five everyday banking flows, a double-entry ledger, and the failure
modes a real money system has to survive. **Sandbox only. No real money, no real identities.**

## The five cases

| # | Case | Failure it demonstrates |
|---|---|---|
| S1 | Get paid: a payroll ACH credit lands in your account | duplicate webhooks |
| S2 | Pull money in from an external bank (ACH debit) | funds are restricted until settled |
| S3 | Send money to a friend instantly | concurrent spends never overdraw |
| S4 | Withdraw to an external bank (ACH credit) | the bank accepts but the response is lost |
| S5 | A pulled deposit is returned as unauthorized | negative balance, freeze, write-off with approval |

## Run it locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
make up        # Postgres in Docker
make migrate   # apply migrations/*.sql
make test      # scenario and failure tests
make dev       # API on http://localhost:8000/docs
make pool SIZE=60   # pre-fund demo spaces so visitors don't wait for payroll
```

Against the Column sandbox, set `FERN_BANK=column` and `FERN_COLUMN_API_KEY` (a `test_` key) in `.env`.
A simulated payroll takes about 2 minutes to land there, so the live demo pre-funds a pool of spaces
(`make pool`) and runs with `FERN_POOL_ONLY=true`. `FERN_RUN_COLUMN=1 uv run pytest tests/test_column_live.py`
runs the five cases on the sandbox and reconciles the ledger against Column's balance.

## Layout

```
migrations/        plain SQL, applied in order
src/fern/
  ledger.py        double-entry posting with per-effect rules
  bank.py          bank client interface + a Column-like simulated bank with fault injection
  payments.py      the five flows, bank-fact handling, recovery of unknown outcomes
  column.py        Column sandbox adapter (same interface as the simulator)
  spaces.py        demo spaces (one isolated book per visitor)
  pool.py          pre-funded pool of spaces, claimed one per visitor
  api.py           FastAPI routes
  platform/        config, database, sessions, idempotency, migrations
tests/
docs/DESIGN.md     full design (Chinese working draft)
```

The simulated bank follows Column's documented ACH behaviour (outgoing credits leave the account
at initiation; pulled debits arrive at settlement; webhooks can repeat and arrive out of order).
The real Column sandbox adapter replaces it once sandbox keys are available.
