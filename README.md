# Fern

A neobank backend demo, live at **https://waken.dev/apps/fern/** until 2026-11-19 (a static archive
after that). Five everyday banking flows on a double-entry ledger, built to survive the failure modes a
real money system meets. **Sandbox only. No real money, no real identities.**

## Architecture

```
Browser ─► Cloudflare (Worker on waken.dev/apps/fern) ─► Cloudflare Tunnel
             └─► AWS EKS: FastAPI (Python 3.13) ─┬─► RDS Postgres 16, Multi-AZ  (ledger)
                                                 ├─► ElastiCache Redis           (rate limits)
                                                 └─► Column (bank, ACH) · Plaid (balances) · OpenAI (ops reports)
```

- **Ledger:** double-entry in Postgres. Every money movement is one transaction with an idempotency key,
  every journal balances, and the books are reconciled against Column's records.
- **Integrations:** Column sandbox for accounts and ACH, Plaid sandbox for linked-account balances,
  OpenAI for the daily ops report (with a rules-based fallback).
- **Infra:** EKS on Graviton, RDS, ElastiCache and ECR, provisioned with OpenTofu. Traffic arrives through a
  Cloudflare Tunnel, so there is no load balancer and no inbound port. Runbook: [docs/DEPLOY.md](docs/DEPLOY.md).
- **Tracing:** every state-changing request records its SQL, bank, Plaid and Redis calls; the demo UI shows
  the trace next to the phone.
- **Load test:** 1,325 req/s within p95 500 ms, 90,222 transfers, 0 money errors ([bench/results](bench/results)).

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
make up        # Postgres and Redis in Docker
make migrate   # apply migrations/*.sql
make test      # scenario and failure tests
make dev       # demo UI on http://localhost:8000/ · API docs on /docs
make pool SIZE=5    # pre-fund demo spaces so visitors don't wait for payroll
```

Against the Column sandbox, set `FERN_BANK=column` and `FERN_COLUMN_API_KEY` (a `test_` key) in `.env`.
A simulated payroll takes about 2 minutes to land there, so the live demo pre-funds a pool of spaces
(`make pool`) and runs with `FERN_POOL_ONLY=true`. `FERN_RUN_COLUMN=1 uv run pytest tests/test_column_live.py`
runs the five cases on the sandbox and reconciles the ledger against Column's balance.

Pulls check the linked account's balance first. With `FERN_PLAID=sandbox` plus `FERN_PLAID_CLIENT_ID` and
`FERN_PLAID_SECRET` that is a real Plaid sandbox Item; the default `sim` uses a fixed balance. Money-moving
requests are rate limited per space through Redis (`FERN_REDIS_URL`, `FERN_RATE_LIMIT_PER_MINUTE`).

Every state-changing request is traced: the response carries `X-Trace-Id`, and `GET /traces/{id}` returns
its spans (each SQL statement, transaction, bank, Plaid and Redis call) for the caller's own space.

## Layout

```
migrations/        plain SQL, applied in order
src/fern/
  ledger.py        double-entry posting with per-effect rules
  bank.py          bank client interface + a Column-like simulated bank with fault injection
  payments.py      the five flows, bank-fact handling, recovery of unknown outcomes
  plaid.py         linked-account balance check before pulls (Plaid sandbox or simulator)
  column.py        Column sandbox adapter (same interface as the simulator)
  spaces.py        demo spaces (one isolated book per visitor)
  pool.py          pre-funded pool of spaces, claimed one per visitor
  api.py           FastAPI routes
  platform/        config, database, sessions, idempotency, migrations, tracing, rate limits
tests/
docs/DESIGN.md     full design (Chinese working draft)
```

The simulated bank follows Column's documented ACH behaviour (outgoing credits leave the account
at initiation; pulled debits arrive at settlement; webhooks can repeat and arrive out of order).
The real Column sandbox adapter replaces it once sandbox keys are available.
