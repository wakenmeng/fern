"""Contract probe against the Column sandbox (DESIGN §6.4, §12.2).

Checks the behaviour the ledger relies on and prints what it saw:
  1. simulated payroll arrives and how long it takes
  2. a pulled debit: status and balances before/after settlement
  3. a pushed credit: does cash leave at initiation?
  4. idempotency: same key + same body, same key + different body
  5. magic-name return (RETURN_UNAUTH): when does the return arrive, with which code?

Usage: uv run python scripts/column_probe.py   (needs FERN_COLUMN_API_KEY in .env)
Creates sandbox objects only. Safe to re-run: setup uses fixed idempotency keys.
"""

import asyncio
import time
import uuid

from fern.bank import BankRejected
from fern.column import ColumnBank, to_snapshot
from fern.platform.config import get_settings

T0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[{time.monotonic() - T0:6.1f}s] {msg}", flush=True)


async def balances(col: ColumnBank, account_id: str) -> dict:
    return (await col.get_bank_account(account_id))["balances"]


async def watch(col: ColumnBank, transfer_id: str, until: set[str], seconds: int) -> dict:
    deadline = time.monotonic() + seconds
    last = None
    while True:
        raw = await col.get_transfer_raw(transfer_id)
        if raw["status"] != last:
            last = raw["status"]
            log(
                f"  {transfer_id} status={last} returned_at={raw.get('returned_at')} "
                f"settled_at={raw.get('settled_at')} return_details={raw.get('return_details')}"
            )
        if last in until or time.monotonic() > deadline:
            return raw
        await asyncio.sleep(3)


async def main() -> None:
    col = ColumnBank(get_settings().column_api_key, get_settings().column_base_url)
    try:
        entity = await col.create_platform_entity("Fern Demo Platform", "fern-probe-entity-v1")
        account = await col.create_bank_account(entity["id"], "fern probe account", "fern-probe-account-v1")
        acct_id, acct_num = account["id"], account["default_account_number_id"]
        log(f"entity={entity['id']} verification={entity.get('verification_status')} account={acct_id}")
        log(f"balances at start: {await balances(col, acct_id)}")

        # 1. payroll
        txn = await col.receive_payroll(2000_00, acct_id, acct_num)
        log(f"simulated payroll arrived and was force-settled: {txn.provider_txn_id} {txn.amount_minor}")
        log(f"balances after payroll: {await balances(col, acct_id)}")

        # 2. pull (outgoing debit)
        key = f"probe-pull-{uuid.uuid4()}"
        s = await col.create_ach_transfer(
            idempotency_key=key,
            direction="debit",
            amount_minor=500_00,
            counterparty_name="External checking",
            account_ref=acct_id,
        )
        log(f"pull created: {s.bank_transfer_id} coarse={s.status} txns={s.transactions}")
        log(f"balances after pull created: {await balances(col, acct_id)}")
        s = await col.settle(s.bank_transfer_id)
        log(f"pull after settle: coarse={s.status} txns={[t.kind for t in s.transactions]}")
        log(f"balances after pull settled: {await balances(col, acct_id)}")

        # 3. withdrawal (outgoing credit)
        key = f"probe-wd-{uuid.uuid4()}"
        s = await col.create_ach_transfer(
            idempotency_key=key,
            direction="credit",
            amount_minor=300_00,
            counterparty_name="External checking",
            account_ref=acct_id,
        )
        log(f"withdrawal created: {s.bank_transfer_id} coarse={s.status} txns={[t.kind for t in s.transactions]}")
        log(f"balances right after withdrawal created: {await balances(col, acct_id)}")

        # 4. idempotency
        again = await col.create_ach_transfer(
            idempotency_key=key,
            direction="credit",
            amount_minor=300_00,
            counterparty_name="External checking",
            account_ref=acct_id,
        )
        log(f"same key + same body -> same transfer: {again.bank_transfer_id == s.bank_transfer_id}")
        try:
            other = await col.create_ach_transfer(
                idempotency_key=key,
                direction="credit",
                amount_minor=301_00,
                counterparty_name="External checking",
                account_ref=acct_id,
            )
            log(
                f"same key + different body -> returned {other.bank_transfer_id} amount={other.amount_minor} "
                f"(same transfer: {other.bank_transfer_id == s.bank_transfer_id})"
            )
        except BankRejected as e:
            log(f"same key + different body -> rejected: {e}")
        s = await col.settle(s.bank_transfer_id)
        log(f"withdrawal after settle: coarse={s.status}; balances {await balances(col, acct_id)}")

        # 5. magic return on a pulled debit
        key = f"probe-return-{uuid.uuid4()}"
        s = await col.create_ach_transfer(
            idempotency_key=key,
            direction="debit",
            amount_minor=100_00,
            counterparty_name="RETURN_UNAUTH",
            account_ref=acct_id,
        )
        log(f"RETURN_UNAUTH pull created: {s.bank_transfer_id}; watching 45s without settling")
        raw = await watch(col, s.bank_transfer_id, {"RETURNED"}, 45)
        if raw["status"] != "RETURNED":
            log("settling it now and watching 90s more")
            try:
                await col.settle(s.bank_transfer_id)
            except BankRejected as e:
                log(f"  settle rejected: {e}")
            raw = await watch(col, s.bank_transfer_id, {"RETURNED"}, 90)
        snap = to_snapshot(raw)
        log(f"final: coarse={snap.status} code={snap.return_code} txns={[t.kind for t in snap.transactions]}")
        log(f"balances at end: {await balances(col, acct_id)}")
    finally:
        await col.aclose()


if __name__ == "__main__":
    asyncio.run(main())
