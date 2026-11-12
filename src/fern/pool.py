"""Pre-funded account pool (DESIGN §2.1).

On the Column sandbox a simulated payroll takes about 2 minutes to land, so visitors can't wait
for it. Before going live we open N spaces, each with its own Column bank account, and have the
payroll credit land on every account. A visitor then claims a ready space, and S1 records the
credit that is already sitting on the account.

    uv run python -m fern.pool --size 60

Safe to re-run: it tops the pool up to --size and finishes spaces left half-provisioned.
"""

import argparse
import asyncio
import time
from collections.abc import Callable
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from fern.bank import Bank
from fern.spaces import NewSpace, create_space, load_space

PAYROLL_MINOR = 2000_00


async def stats(conn: AsyncConnection) -> dict[str, int]:
    cur = await conn.execute("SELECT pool_status, count(*) AS n FROM spaces GROUP BY pool_status")
    counts = {r["pool_status"]: r["n"] for r in await cur.fetchall()}
    return {k: counts.get(k, 0) for k in ("provisioning", "ready", "claimed")}


async def claim(conn: AsyncConnection) -> NewSpace | None:
    """Hand the oldest ready space to a visitor. Concurrent claims never get the same space."""
    cur = await conn.execute(
        "UPDATE spaces SET pool_status = 'claimed', claimed_at = now() WHERE id = ("
        "  SELECT id FROM spaces WHERE pool_status = 'ready' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
        ") RETURNING id"
    )
    row = await cur.fetchone()
    return await load_space(conn, row["id"]) if row else None


async def provision(
    conn: AsyncConnection,
    bank: Bank,
    size: int,
    *,
    payroll_minor: int = PAYROLL_MINOR,
    poll_seconds: float = 10,
    timeout_seconds: float = 600,
    log: Callable[[str], Any] = print,
) -> dict[str, int]:
    before = await stats(conn)
    missing = size - before["provisioning"] - before["ready"]
    for i in range(max(missing, 0)):
        await create_space(conn, bank, pool_status="provisioning")
        log(f"opened space {i + 1}/{missing}")

    # Ask for every payroll first, then collect them: the ~2 minute wait overlaps for all spaces.
    cur = await conn.execute(
        "SELECT id, bank_account_id, bank_account_number_id FROM spaces "
        "WHERE pool_status = 'provisioning' AND payroll_requested_at IS NULL"
    )
    for s in await cur.fetchall():
        await bank.request_incoming_credit(payroll_minor, s["bank_account_id"], s["bank_account_number_id"])
        await conn.execute("UPDATE spaces SET payroll_requested_at = now() WHERE id = %s", (s["id"],))

    deadline = time.monotonic() + timeout_seconds
    while True:
        cur = await conn.execute("SELECT id, bank_account_id FROM spaces WHERE pool_status = 'provisioning'")
        pending: list[dict[str, Any]] = await cur.fetchall()
        for s in pending:
            txn = await bank.find_incoming_credit(s["bank_account_id"])
            if txn is None:
                continue
            await conn.execute(
                "UPDATE spaces SET pool_status = 'ready', payroll_txn_id = %s, payroll_amount_minor = %s, "
                "payroll_posted_at = %s WHERE id = %s AND pool_status = 'provisioning'",
                (txn.provider_txn_id, txn.amount_minor, txn.posted_at, s["id"]),
            )
            log(f"space {s['id']} ready")
        remaining = (await stats(conn))["provisioning"]
        if remaining == 0 or time.monotonic() > deadline:
            break
        log(f"waiting for {remaining} payroll credit(s) to land")
        await asyncio.sleep(poll_seconds)
    return await stats(conn)


async def _main() -> None:
    from fern.api import make_bank
    from fern.platform.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=60)
    args = parser.parse_args()
    settings = get_settings()
    bank = make_bank(settings)
    async with await AsyncConnection.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        print(await provision(conn, bank, args.size))
    if hasattr(bank, "aclose"):
        await bank.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
