"""Reconciliation: compare the bank's record of a space's account with our ledger (DESIGN §7).

Read only. A run never changes the books; it records discrepancies as exceptions, which stay
open until a later run no longer sees them (resolved) or a person escalates them.

Bank side:
  * Column: every ACH transfer on the space's account, turned into cash movements with the same
    stable ids the payment flows use (fern.column.to_snapshot), plus settled incoming credits.
  * Simulator: it has no per-account listing, so we re-read every transfer we know about (ours
    and the ones a drill created) from it.
Book side: bank_transactions rows, each of which must be posted by exactly one journal.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection

from fern import ledger
from fern.bank import Bank, BankTxn
from fern.column import _SETTLED, ColumnBank, to_snapshot
from fern.platform.db import transaction


@dataclass(frozen=True)
class Finding:
    kind: str
    provider_txn_id: str
    amount_minor: int
    detail: str


async def _space(conn: AsyncConnection, space_id: UUID) -> dict[str, Any]:
    cur = await conn.execute("SELECT * FROM spaces WHERE id = %s", (space_id,))
    return await cur.fetchone()


async def _column_side(bank: ColumnBank, account_id: str) -> tuple[dict[str, BankTxn], dict[str, str], int]:
    """Cash movements at Column, which of our transfers each belongs to, and the account balance."""
    data = await bank._request("GET", "/transfers/ach", params={"bank_account_id": account_id, "limit": 100})
    txns: dict[str, BankTxn] = {}
    owner: dict[str, str] = {}
    for t in data.get("transfers", []):
        if t.get("is_incoming"):
            if t.get("type") == "CREDIT" and t["status"] in _SETTLED:
                txns[t["id"]] = BankTxn(t["id"], int(t["amount"]), "incoming", None)  # type: ignore[arg-type]
            continue
        for x in to_snapshot(t).transactions:
            txns[x.provider_txn_id] = x
            # Our idempotency key is the transfer id, and we put it in the description.
            owner[x.provider_txn_id] = (t.get("description") or "").removeprefix("fern ").strip()
    account = await bank.get_bank_account(account_id)
    balances = account["balances"]
    # Cash on the account. A pulled debit sits in pending_amount until it settles, and our ledger
    # only books it then (sandbox probe 2026-11-12), so pending is not compared.
    return txns, owner, int(balances["available_amount"])


async def _sim_side(conn: AsyncConnection, bank: Any, space_id: UUID) -> tuple[dict[str, BankTxn], dict[str, str]]:
    txns: dict[str, BankTxn] = {}
    owner: dict[str, str] = {}
    cur = await conn.execute(
        "SELECT id::text AS id, bank_transfer_id FROM transfers WHERE space_id = %s AND kind IN ('pull', 'withdrawal') "
        "UNION ALL SELECT NULL, bank_transfer_id FROM drill_bank_transfers WHERE space_id = %s",
        (space_id, space_id),
    )
    for row in await cur.fetchall():
        bank_id = row["bank_transfer_id"] or bank.by_key.get(row["id"])  # unknown outcome: look up by key
        if not bank_id:
            continue
        for x in (await bank.get_transfer(bank_id)).transactions:
            txns[x.provider_txn_id] = x
            owner[x.provider_txn_id] = row["id"] or "drill"
    # Incoming credits (payroll) have no transfer to re-read on the simulator; take our record of them.
    cur = await conn.execute(
        "SELECT provider_txn_id, amount_minor FROM bank_transactions WHERE space_id = %s AND kind = 'incoming'",
        (space_id,),
    )
    for row in await cur.fetchall():
        txns[row["provider_txn_id"]] = BankTxn(row["provider_txn_id"], row["amount_minor"], "incoming", None)  # type: ignore[arg-type]
    return txns, owner


async def run(conn: AsyncConnection, bank: Bank, space_id: UUID) -> dict[str, Any]:
    space = await _space(conn, space_id)
    bank_balance: int | None = None
    if isinstance(bank, ColumnBank) and space["bank_account_id"]:
        at_bank, owner, bank_balance = await _column_side(bank, space["bank_account_id"])
    else:
        at_bank, owner = await _sim_side(conn, bank, space_id)

    cur = await conn.execute(
        "SELECT bt.provider_txn_id, bt.amount_minor, count(j.id) AS journals FROM bank_transactions bt "
        "LEFT JOIN journals j ON j.bank_transaction_id = bt.id WHERE bt.space_id = %s "
        "GROUP BY bt.provider_txn_id, bt.amount_minor",
        (space_id,),
    )
    in_books = {r["provider_txn_id"]: r for r in await cur.fetchall()}
    cur = await conn.execute("SELECT id::text AS id, status FROM transfers WHERE space_id = %s", (space_id,))
    status_of = {r["id"]: r["status"] for r in await cur.fetchall()}
    book_balance = ledger.normalized("bank", (await _bank_account(conn, space_id))["raw_sum_minor"])

    findings: list[Finding] = []
    matched = 0
    for txn_id, x in at_bank.items():
        ours = in_books.get(txn_id)
        if ours is None:
            if txn_id == space["payroll_txn_id"]:
                continue  # pooled payroll already at the bank; S1 records it when the visitor asks
            if status_of.get(owner.get(txn_id, "")) == "unknown":
                findings.append(
                    Finding(
                        "in_flight_unconfirmed",
                        txn_id,
                        x.amount_minor,
                        "The bank moved money for a transfer whose outcome we haven't confirmed. "
                        "The recovery job's same-key retry will post it.",
                    )
                )
            else:
                findings.append(Finding("missing_in_books", txn_id, x.amount_minor, "At the bank, not in our books."))
        elif ours["amount_minor"] != x.amount_minor:
            findings.append(
                Finding(
                    "amount_mismatch", txn_id, x.amount_minor, f"Bank {x.amount_minor}, books {ours['amount_minor']}."
                )
            )
        else:
            matched += 1
    for txn_id, ours in in_books.items():
        if txn_id not in at_bank:
            findings.append(
                Finding(
                    "missing_at_bank",
                    txn_id,
                    ours["amount_minor"],
                    "In our books, but the bank shows no such movement.",
                )
            )
        elif ours["journals"] != 1:
            findings.append(
                Finding(
                    "amount_mismatch",
                    txn_id,
                    ours["amount_minor"],
                    f"Posted {ours['journals']} times; must be exactly once.",
                )
            )

    # Balances: once every movement is explained, the totals must agree too.
    pending_payroll = (
        space["payroll_amount_minor"] if space["payroll_txn_id"] and space["payroll_txn_id"] not in in_books else 0
    )
    if bank_balance is not None and bank_balance - (pending_payroll or 0) != book_balance:
        findings.append(
            Finding(
                "balance_mismatch",
                "balance",
                bank_balance - book_balance,
                f"Bank {bank_balance}, books {book_balance}"
                + (f", payroll awaiting S1 {pending_payroll}" if pending_payroll else "")
                + ".",
            )
        )

    async def record(c: AsyncConnection) -> dict[str, Any]:
        run_id = uuid4()
        open_now = [f for f in findings if f.kind != "in_flight_unconfirmed"]
        await c.execute(
            "INSERT INTO recon_runs (id, space_id, provider, bank_balance, book_balance, scanned, matched, "
            "open_exceptions) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, space_id, bank.provider, bank_balance, book_balance, len(at_bank), matched, len(open_now)),
        )
        seen = []
        for f in findings:
            cur = await c.execute(
                "INSERT INTO recon_exceptions (id, space_id, kind, provider_txn_id, amount_minor, detail, "
                "first_run_id, last_run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (space_id, kind, provider_txn_id) DO UPDATE SET last_run_id = EXCLUDED.last_run_id, "
                "amount_minor = EXCLUDED.amount_minor, detail = EXCLUDED.detail, "
                "status = CASE WHEN recon_exceptions.status = 'resolved' THEN 'open' ELSE recon_exceptions.status END "
                "RETURNING id",
                (uuid4(), space_id, f.kind, f.provider_txn_id, f.amount_minor, f.detail, run_id, run_id),
            )
            seen.append((await cur.fetchone())["id"])
        # Anything open that this run no longer sees has been explained, e.g. the retry posted it.
        await c.execute(
            "UPDATE recon_exceptions SET status = 'resolved', resolved_at = now(), "
            "note = coalesce(note, 'No longer seen by reconciliation') "
            "WHERE space_id = %s AND status = 'open' AND NOT (id = ANY(%s))",
            (space_id, seen),
        )
        return {"run_id": run_id}

    await transaction(conn, record)
    return await latest(conn, space_id)


async def _bank_account(conn: AsyncConnection, space_id: UUID) -> dict[str, Any]:
    cur = await conn.execute("SELECT raw_sum_minor FROM accounts WHERE space_id = %s AND role = 'bank'", (space_id,))
    return await cur.fetchone()


async def latest(conn: AsyncConnection, space_id: UUID) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT * FROM recon_runs WHERE space_id = %s ORDER BY started_at DESC LIMIT 1", (space_id,)
    )
    last = await cur.fetchone()
    cur = await conn.execute(
        "SELECT id, kind, provider_txn_id, amount_minor, detail, status, note, created_at, resolved_at "
        "FROM recon_exceptions WHERE space_id = %s ORDER BY created_at DESC LIMIT 50",
        (space_id,),
    )
    return {"last_run": last, "exceptions": await cur.fetchall()}


async def escalate(conn: AsyncConnection, space_id: UUID, exception_id: UUID, note: str) -> dict[str, Any]:
    cur = await conn.execute(
        "UPDATE recon_exceptions SET status = 'escalated', note = %s WHERE id = %s AND space_id = %s RETURNING *",
        (note, exception_id, space_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise LookupError("no such exception")
    return row


async def drill_unbooked_bank_debit(
    conn: AsyncConnection, bank: Bank, space_id: UUID, amount_minor: int = 25_00
) -> str:
    """Drill: money leaves the space's bank account without our ledger knowing (a bank fee, a rogue
    payment). Reconciliation must catch it. On Column this is a real sandbox transfer."""
    space = await _space(conn, space_id)
    key = f"drill-unbooked-{uuid4()}"
    snapshot = await bank.create_ach_transfer(
        idempotency_key=key,
        direction="credit",
        amount_minor=amount_minor,
        counterparty_name="Unknown vendor",
        account_ref=space["bank_account_id"],
    )
    await conn.execute(
        "INSERT INTO drill_bank_transfers (bank_transfer_id, space_id) VALUES (%s, %s)",
        (snapshot.bank_transfer_id, space_id),
    )
    return snapshot.bank_transfer_id
