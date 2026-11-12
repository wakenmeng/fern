"""The five flows (DESIGN §1) and how bank facts turn into ledger entries (DESIGN §5).

Rules that keep the money right:
  * Every change to a customer's spendable money locks their ledger account first, then reads
    balances, then writes. Two requests can never both spend the same dollars.
  * Bank calls happen outside database transactions. Their results are applied afterwards.
  * Bank results are applied by recomputing *all* entries the bank's current state implies.
    The ledger skips entries it already has, so duplicates and out-of-order webhooks are harmless.
  * If we don't know whether the bank created a transfer, the transfer goes to `unknown` and the
    money stays held. Recovery retries with the same idempotency key.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from fern import ledger
from fern.bank import Bank, BankConflict, BankEvent, BankOutcomeUnknown, BankRejected, BankTxn, TransferSnapshot
from fern.ledger import Entry
from fern.plaid import PlaidError, SourceAccount
from fern.platform import idempotency, tracing
from fern.platform.config import get_settings
from fern.platform.db import transaction
from fern.platform.idempotency import Response


class PaymentError(Exception):
    status_code = 422


class InsufficientFunds(PaymentError):
    pass


class NotFound(PaymentError):
    status_code = 404


# --------------------------------------------------------------------------- reads


async def balances(conn: AsyncConnection, space_id: UUID, customer_id: UUID) -> dict[str, int]:
    cur = await conn.execute(
        """
        SELECT
          (SELECT -raw_sum_minor FROM accounts
            WHERE space_id = %(s)s AND role = 'customer_deposit' AND customer_id = %(c)s) AS deposit,
          (SELECT coalesce(sum(amount_minor), 0) FROM holds
            WHERE customer_id = %(c)s AND status = 'active') AS held,
          (SELECT coalesce(sum(amount_minor), 0) FROM restrictions
            WHERE customer_id = %(c)s AND status = 'active') AS restricted
        """,
        {"s": space_id, "c": customer_id},
    )
    row = await cur.fetchone()
    if row is None or row["deposit"] is None:
        raise NotFound("customer not found in this space")
    deposit, held, restricted = int(row["deposit"]), int(row["held"]), int(row["restricted"])
    return {
        "balance_minor": deposit,
        "held_minor": held,
        "restricted_minor": restricted,
        "available_minor": deposit - held - restricted,
    }


async def get_transfer(conn: AsyncConnection, space_id: UUID, transfer_id: UUID) -> dict[str, Any]:
    cur = await conn.execute("SELECT * FROM transfers WHERE id = %s AND space_id = %s", (transfer_id, space_id))
    t = await cur.fetchone()
    if t is None:
        raise NotFound("transfer not found")
    return t


async def transfer_view(conn: AsyncConnection, space_id: UUID, transfer_id: UUID) -> dict[str, Any]:
    """Everything about one transfer: state, timeline, and the journals it produced."""
    t = await get_transfer(conn, space_id, transfer_id)
    events = await (
        await conn.execute(
            "SELECT event, detail, occurred_at FROM transfer_events WHERE transfer_id = %s ORDER BY id",
            (transfer_id,),
        )
    ).fetchall()
    journals = await (
        await conn.execute(
            "SELECT j.id, j.effect, j.amount_minor, d.role AS debit, c.role AS credit, "
            "bt.provider_txn_id AS bank_txn, j.created_at FROM journals j "
            "JOIN accounts d ON d.id = j.debit_account_id JOIN accounts c ON c.id = j.credit_account_id "
            "LEFT JOIN bank_transactions bt ON bt.id = j.bank_transaction_id "
            "WHERE j.transfer_id = %s ORDER BY j.created_at, j.effect",
            (transfer_id,),
        )
    ).fetchall()
    return {"transfer": _public(t), "timeline": events, "journals": journals}


def _public(t: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "kind",
        "status",
        "amount_minor",
        "from_customer_id",
        "to_customer_id",
        "bank_transfer_id",
        "return_code",
        "failure_reason",
        "created_at",
    )
    return {k: t[k] for k in keys}


# --------------------------------------------------------------------------- helpers


async def _event(conn: AsyncConnection, transfer_id: UUID, event: str, **detail: Any) -> None:
    await conn.execute(
        "INSERT INTO transfer_events (transfer_id, event, detail) VALUES (%s, %s, %s)",
        (transfer_id, event, Jsonb(detail)),
    )


async def _lock_and_check(
    conn: AsyncConnection, space_id: UUID, customer_id: UUID, amount: int, *also_lock: UUID
) -> UUID:
    """Lock the customer's account (and any others), then check they can spend `amount`."""
    deposit = await ledger.account_id(conn, space_id, "customer_deposit", customer_id)
    await ledger.lock_accounts(conn, space_id, [deposit, *also_lock])
    available = (await balances(conn, space_id, customer_id))["available_minor"]
    if available < amount:
        raise InsufficientFunds(f"available balance is {available}, need {amount}")
    return deposit


async def _idempotent(
    conn: AsyncConnection,
    space_id: UUID,
    principal: str,
    operation: str,
    key: str,
    body: dict[str, Any],
    work: Callable[[AsyncConnection], Awaitable[Response]],
) -> Response:
    """Run `work` once per idempotency key. Business errors are stored and replayed too."""
    req_hash = idempotency.request_hash(body)

    async def tx(c: AsyncConnection) -> Response:
        ids = {"space_id": space_id, "principal": principal, "operation": operation, "key": key}
        stored = await idempotency.claim(c, req_hash=req_hash, **ids)
        if stored is not None:
            return stored
        try:
            async with c.transaction():  # savepoint: a business error undoes only the work
                response = await work(c)
        except PaymentError as e:
            response = Response(e.status_code, {"error": type(e).__name__, "message": str(e)})
        await idempotency.save(c, response=response, **ids)
        return response

    # Name the transaction after the flow (request_withdrawal), not this wrapper.
    return await transaction(conn, tx, name=work.__qualname__.split(".<locals>")[0])


def _created(t: dict[str, Any]) -> Response:
    return Response(201, {k: str(v) if v is not None else None for k, v in _public(t).items()})


async def _insert_transfer(
    conn: AsyncConnection,
    space_id: UUID,
    kind: str,
    status: str,
    amount: int,
    *,
    from_customer: UUID | None = None,
    to_customer: UUID | None = None,
    counterparty_name: str | None = None,
) -> dict[str, Any]:
    cur = await conn.execute(
        "INSERT INTO transfers (id, space_id, kind, status, amount_minor, from_customer_id, to_customer_id, "
        "counterparty_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (uuid4(), space_id, kind, status, amount, from_customer, to_customer, counterparty_name),
    )
    t = await cur.fetchone()
    assert t is not None
    return t


# --------------------------------------------------------------------------- S1: payroll


async def record_incoming_credit(
    conn: AsyncConnection, provider: str, space_id: UUID, customer_id: UUID, txn: BankTxn
) -> UUID:
    """An incoming ACH credit posted at the bank. Recording the same bank transaction twice is a no-op."""

    async def work(c: AsyncConnection) -> UUID:
        cur = await c.execute(
            "INSERT INTO bank_transactions (id, space_id, provider, provider_txn_id, amount_minor, kind, posted_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (provider, provider_txn_id) DO NOTHING RETURNING id",
            (uuid4(), space_id, provider, txn.provider_txn_id, txn.amount_minor, txn.kind, txn.posted_at),
        )
        row = await cur.fetchone()
        if row is None:  # already recorded
            cur = await c.execute(
                "SELECT j.transfer_id FROM journals j JOIN bank_transactions bt ON bt.id = j.bank_transaction_id "
                "WHERE bt.provider = %s AND bt.provider_txn_id = %s",
                (provider, txn.provider_txn_id),
            )
            existing = await cur.fetchone()
            assert existing is not None
            return existing["transfer_id"]

        t = await _insert_transfer(c, space_id, "payroll", "completed", txn.amount_minor, to_customer=customer_id)
        bank_acct = await ledger.account_id(c, space_id, "bank")
        deposit = await ledger.account_id(c, space_id, "customer_deposit", customer_id)
        await ledger.post(
            c,
            space_id,
            [
                Entry(
                    "ach_in.credit",
                    txn.amount_minor,
                    bank_acct,
                    deposit,
                    f"{t['id']}:ach_in.credit",
                    transfer_id=t["id"],
                    bank_transaction_id=row["id"],
                )
            ],
        )
        await _event(c, t["id"], "payroll_received", bank_txn=txn.provider_txn_id)
        return t["id"]

    return await transaction(conn, work)


# --------------------------------------------------------------------------- S3: P2P


async def send_p2p(
    conn: AsyncConnection,
    space_id: UUID,
    principal: str,
    idem_key: str,
    *,
    from_customer: UUID,
    to_customer: UUID,
    amount_minor: int,
) -> Response:
    body = {"to": str(to_customer), "amount_minor": amount_minor}

    async def work(c: AsyncConnection) -> Response:
        if from_customer == to_customer:
            raise PaymentError("cannot send money to yourself")
        payee = await ledger.account_id(c, space_id, "customer_deposit", to_customer)
        payer = await _lock_and_check(c, space_id, from_customer, amount_minor, payee)
        t = await _insert_transfer(
            c,
            space_id,
            "p2p",
            "completed",
            amount_minor,
            from_customer=from_customer,
            to_customer=to_customer,
        )
        await ledger.post(
            c,
            space_id,
            [Entry("p2p.transfer", amount_minor, payer, payee, f"{t['id']}:p2p.transfer", transfer_id=t["id"])],
        )
        await _event(c, t["id"], "completed")
        return _created(t)

    return await _idempotent(conn, space_id, principal, "p2p", idem_key, body, work)


# --------------------------------------------------------------------------- S4: withdrawal


async def request_withdrawal(
    conn: AsyncConnection,
    space_id: UUID,
    principal: str,
    idem_key: str,
    *,
    customer_id: UUID,
    amount_minor: int,
) -> Response:
    """Hold the money. The bank call happens afterwards in `submit_to_bank`."""
    body = {"amount_minor": amount_minor}

    async def work(c: AsyncConnection) -> Response:
        await _lock_and_check(c, space_id, customer_id, amount_minor)
        t = await _insert_transfer(
            c,
            space_id,
            "withdrawal",
            "held",
            amount_minor,
            from_customer=customer_id,
            counterparty_name="External checking",
        )
        await c.execute(
            "INSERT INTO holds (transfer_id, customer_id, amount_minor, status) VALUES (%s, %s, %s, 'active')",
            (t["id"], customer_id, amount_minor),
        )
        await _event(c, t["id"], "hold_placed", amount_minor=amount_minor)
        return _created(t)

    return await _idempotent(conn, space_id, principal, "withdrawal", idem_key, body, work)


# --------------------------------------------------------------------------- S2 / S5: pull


async def request_pull(
    conn: AsyncConnection,
    space_id: UUID,
    principal: str,
    idem_key: str,
    *,
    customer_id: UUID,
    amount_minor: int,
    counterparty_name: str = "External checking",
) -> Response:
    """Pull money from the customer's linked external account (an ACH debit we originate)."""
    body = {"amount_minor": amount_minor, "counterparty": counterparty_name}

    async def work(c: AsyncConnection) -> Response:
        t = await _insert_transfer(
            c,
            space_id,
            "pull",
            "pending",
            amount_minor,
            to_customer=customer_id,
            counterparty_name=counterparty_name,
        )
        await _event(c, t["id"], "debit_authorized", authorization="demo-ach-debit-v1")
        return _created(t)

    return await _idempotent(conn, space_id, principal, "pull", idem_key, body, work)


# --------------------------------------------------------------------------- bank interaction


async def submit_to_bank(
    conn: AsyncConnection, bank: Bank, space_id: UUID, transfer_id: UUID, source: SourceAccount | None = None
) -> dict[str, Any]:
    """Send a withdrawal or pull to the bank. Safe to call again: it reuses the same idempotency key.

    A new pull first checks the source account's balance with Plaid (when `source` is given)."""
    t = await get_transfer(conn, space_id, transfer_id)
    if t["status"] not in ("held", "pending", "unknown"):
        return t
    if source is not None and t["kind"] == "pull" and t["status"] == "pending":
        shortfall = await _source_shortfall(conn, source, space_id, t)
        if shortfall:
            await transaction(conn, lambda c: _fail(c, t, shortfall, event="source_check_failed"))
            return await get_transfer(conn, space_id, transfer_id)
    cur = await conn.execute("SELECT bank_account_id FROM spaces WHERE id = %s", (space_id,))
    account_ref = (await cur.fetchone())["bank_account_id"]
    try:
        snapshot = await bank.create_ach_transfer(
            idempotency_key=str(transfer_id),
            direction="credit" if t["kind"] == "withdrawal" else "debit",
            amount_minor=t["amount_minor"],
            counterparty_name=t["counterparty_name"] or "External checking",
            account_ref=account_ref,
        )
    except BankRejected as exc:
        reason = str(exc)
        await transaction(conn, lambda c: _fail(c, t, reason))
    except BankOutcomeUnknown as exc:
        reason = str(exc)
        await transaction(conn, lambda c: _mark_unknown(c, t, reason))
    except BankConflict as exc:
        reason = str(exc)
        await transaction(conn, lambda c: _manual_review(c, t, reason))
    else:
        await apply_snapshot(conn, bank.provider, space_id, transfer_id, snapshot)
    return await get_transfer(conn, space_id, transfer_id)


async def _source_shortfall(conn: AsyncConnection, source: SourceAccount, space_id: UUID, t: dict[str, Any]) -> str:
    """Why the linked account can't fund this pull, or "" if it can."""
    cur = await conn.execute("SELECT plaid_access_token FROM spaces WHERE id = %s", (space_id,))
    token = (await cur.fetchone())["plaid_access_token"]
    try:
        if token is None:
            token = await source.link()
            await conn.execute("UPDATE spaces SET plaid_access_token = %s WHERE id = %s", (token, space_id))
        available = await source.available_balance(token)
    except PlaidError as exc:
        return f"could not check the linked account: {exc}"
    if available < t["amount_minor"]:
        return f"linked account has {available} cents available, the pull needs {t['amount_minor']}"
    return ""


async def _fail(c: AsyncConnection, t: dict[str, Any], reason: str, event: str = "bank_rejected") -> None:
    await c.execute("UPDATE holds SET status = 'released' WHERE transfer_id = %s AND status = 'active'", (t["id"],))
    await c.execute(
        "UPDATE transfers SET status = 'failed', failure_reason = %s, updated_at = now() WHERE id = %s",
        (reason, t["id"]),
    )
    await _event(c, t["id"], event, reason=reason)


async def _mark_unknown(c: AsyncConnection, t: dict[str, Any], reason: str) -> None:
    # The money stays held. Only a definite answer from the bank may release it.
    await c.execute(
        "UPDATE transfers SET status = 'unknown', updated_at = now() WHERE id = %s AND status IN ('held', 'pending')",
        (t["id"],),
    )
    await _event(c, t["id"], "bank_outcome_unknown", reason=reason)


async def _manual_review(c: AsyncConnection, t: dict[str, Any], reason: str) -> None:
    # Never guess with money: a person has to look. The hold stays in place.
    await c.execute("UPDATE transfers SET status = 'manual_review', updated_at = now() WHERE id = %s", (t["id"],))
    await _event(c, t["id"], "manual_review", reason=reason)


async def recover_unknown(conn: AsyncConnection, bank: Bank, space_id: UUID) -> list[UUID]:
    """Retry every transfer whose bank outcome is unknown, with its original idempotency key."""
    cur = await conn.execute("SELECT id FROM transfers WHERE space_id = %s AND status = 'unknown'", (space_id,))
    ids = [r["id"] for r in await cur.fetchall()]
    for tid in ids:
        await submit_to_bank(conn, bank, space_id, tid)
    return ids


async def handle_bank_event(conn: AsyncConnection, bank: Bank, event: BankEvent) -> str:
    """A webhook. It is only a hint that something changed: we fetch the transfer from the bank and
    apply its current state, so repeated, late, or out-of-order events are all harmless."""
    cur = await conn.execute(
        "SELECT 1 FROM bank_events WHERE provider = %s AND event_id = %s", (bank.provider, event.event_id)
    )
    if await cur.fetchone():
        return "duplicate"
    cur = await conn.execute(
        "SELECT id, space_id FROM transfers WHERE bank_transfer_id = %s", (event.bank_transfer_id,)
    )
    t = await cur.fetchone()
    if t is None:
        return "unknown_transfer"
    tracing.set_space(t["space_id"])
    snapshot = await bank.get_transfer(event.bank_transfer_id)
    await apply_snapshot(conn, bank.provider, t["space_id"], t["id"], snapshot)
    # Mark the event done only after it was applied, so a crash in between means a harmless redo.
    await conn.execute(
        "INSERT INTO bank_events (provider, event_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (bank.provider, event.event_id),
    )
    return "applied"


_STATUS = {"initiated": "processing", "settled": "settled", "returned": "returned", "canceled": "canceled"}


async def apply_snapshot(
    conn: AsyncConnection, provider: str, space_id: UUID, transfer_id: UUID, snapshot: TransferSnapshot
) -> None:
    """Bring our records in line with what the bank says about this transfer."""

    async def work(c: AsyncConnection) -> None:
        cur = await c.execute(
            "SELECT * FROM transfers WHERE id = %s AND space_id = %s FOR UPDATE", (transfer_id, space_id)
        )
        t = await cur.fetchone()
        if t is None:
            raise NotFound("transfer not found")
        tid, amount = t["id"], t["amount_minor"]

        txn_ids: dict[str, UUID] = {}
        for x in snapshot.transactions:
            await c.execute(
                "INSERT INTO bank_transactions (id, space_id, provider, provider_txn_id, amount_minor, kind, "
                "posted_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (provider, provider_txn_id) DO NOTHING",
                (uuid4(), space_id, provider, x.provider_txn_id, x.amount_minor, x.kind, x.posted_at),
            )
            cur = await c.execute(
                "SELECT id FROM bank_transactions WHERE provider = %s AND provider_txn_id = %s",
                (provider, x.provider_txn_id),
            )
            txn_ids[x.provider_txn_id] = (await cur.fetchone())["id"]

        bank_acct = await ledger.account_id(c, space_id, "bank")
        entries: list[Entry] = []
        if t["kind"] == "withdrawal":
            deposit = await ledger.account_id(c, space_id, "customer_deposit", t["from_customer_id"])
            pending = await ledger.account_id(c, space_id, "ach_credit_pending")
            entries.append(Entry("ach_credit.accept", amount, deposit, pending, f"{tid}:ach_credit.accept", tid))
            for x in snapshot.transactions:
                if x.kind == "initiation":
                    entries.append(
                        Entry(
                            "ach_credit.cash_out",
                            amount,
                            pending,
                            bank_acct,
                            f"{tid}:ach_credit.cash_out",
                            tid,
                            txn_ids[x.provider_txn_id],
                        )
                    )
                elif x.kind == "return":
                    entries.append(
                        Entry(
                            "ach_credit.return",
                            amount,
                            bank_acct,
                            deposit,
                            f"{tid}:ach_credit.return",
                            tid,
                            txn_ids[x.provider_txn_id],
                        )
                    )
        else:
            deposit = await ledger.account_id(c, space_id, "customer_deposit", t["to_customer_id"])
            receivable = await ledger.account_id(c, space_id, "ach_debit_receivable")
            entries.append(Entry("ach_debit.accept", amount, receivable, deposit, f"{tid}:ach_debit.accept", tid))
            for x in snapshot.transactions:
                if x.kind == "settlement":
                    entries.append(
                        Entry(
                            "ach_debit.settle",
                            amount,
                            bank_acct,
                            receivable,
                            f"{tid}:ach_debit.settle",
                            tid,
                            txn_ids[x.provider_txn_id],
                        )
                    )
                elif x.kind == "return":
                    entries.append(
                        Entry(
                            "ach_debit.return",
                            amount,
                            deposit,
                            bank_acct,
                            f"{tid}:ach_debit.return",
                            tid,
                            txn_ids[x.provider_txn_id],
                        )
                    )
            if snapshot.status in ("returned", "canceled") and not any(
                x.kind == "settlement" for x in snapshot.transactions
            ):
                # The money never arrived: undo the credit we gave the customer when the bank accepted it.
                entries.append(
                    Entry(
                        "ach_debit.accept_reversal",
                        amount,
                        deposit,
                        receivable,
                        f"{tid}:ach_debit.accept_reversal",
                        tid,
                    )
                )

        await ledger.post(c, space_id, entries)

        if t["kind"] == "withdrawal":
            # The accept entry moved the money out of the deposit; the hold has done its job.
            await c.execute("UPDATE holds SET status = 'consumed' WHERE transfer_id = %s AND status = 'active'", (tid,))
        else:
            await _update_restriction(c, t, snapshot)

        new_status = _STATUS[snapshot.status]
        if t["status"] != new_status or t["bank_transfer_id"] is None:
            await c.execute(
                "UPDATE transfers SET status = %s, bank_transfer_id = %s, return_code = %s, updated_at = now() "
                "WHERE id = %s",
                (new_status, snapshot.bank_transfer_id, snapshot.return_code, tid),
            )
        if t["status"] != new_status:
            await _event(
                c,
                tid,
                new_status,
                bank_transfer_id=snapshot.bank_transfer_id,
                return_code=snapshot.return_code,
            )
            if new_status == "returned" and t["kind"] == "pull":
                b = await balances(c, space_id, t["to_customer_id"])
                if b["balance_minor"] < 0:
                    await _event(c, tid, "account_negative", balance_minor=b["balance_minor"])

    await transaction(conn, work)


async def _update_restriction(c: AsyncConnection, t: dict[str, Any], snapshot: TransferSnapshot) -> None:
    """Pulled money can't be spent until it settles and the hold period passes (DESIGN §5.2)."""
    await c.execute(
        "INSERT INTO restrictions (transfer_id, customer_id, amount_minor, status) "
        "VALUES (%s, %s, %s, 'active') ON CONFLICT DO NOTHING",
        (t["id"], t["to_customer_id"], t["amount_minor"]),
    )
    settled = [x.posted_at for x in snapshot.transactions if x.kind == "settlement"]
    if settled:
        release_at = settled[0] + timedelta(seconds=get_settings().pull_hold_seconds)
        await c.execute(
            "UPDATE restrictions SET release_at = %s WHERE transfer_id = %s AND release_at IS NULL",
            (release_at, t["id"]),
        )
    if snapshot.status in ("returned", "canceled"):
        await c.execute(
            "UPDATE restrictions SET status = 'terminated' WHERE transfer_id = %s AND status = 'active'",
            (t["id"],),
        )


async def release_restrictions(conn: AsyncConnection, space_id: UUID, now: datetime | None = None) -> int:
    """Make pulled money spendable once its hold period has passed."""
    cur = await conn.execute(
        "UPDATE restrictions SET status = 'released' WHERE status = 'active' AND release_at <= %s "
        "AND customer_id IN (SELECT id FROM customers WHERE space_id = %s) RETURNING transfer_id",
        (now or datetime.now(UTC), space_id),
    )
    rows = await cur.fetchall()
    for r in rows:
        await _event(conn, r["transfer_id"], "funds_available")
    return len(rows)


# --------------------------------------------------------------------------- S5: write-off


async def request_write_off(conn: AsyncConnection, space_id: UUID, customer_id: UUID, requested_by: str) -> dict:
    b = await balances(conn, space_id, customer_id)
    if b["balance_minor"] >= 0:
        raise PaymentError("nothing to write off: balance is not negative")
    cur = await conn.execute(
        "INSERT INTO write_offs (id, space_id, customer_id, amount_minor, status, requested_by) "
        "VALUES (%s, %s, %s, %s, 'pending', %s) RETURNING *",
        (uuid4(), space_id, customer_id, -b["balance_minor"], requested_by),
    )
    return await cur.fetchone()


async def approve_write_off(conn: AsyncConnection, space_id: UUID, write_off_id: UUID, approved_by: str) -> dict:
    async def work(c: AsyncConnection) -> dict:
        cur = await c.execute(
            "SELECT * FROM write_offs WHERE id = %s AND space_id = %s FOR UPDATE", (write_off_id, space_id)
        )
        w = await cur.fetchone()
        if w is None:
            raise NotFound("write-off not found")
        if w["status"] == "approved":
            return w
        if w["requested_by"] == approved_by:
            raise PaymentError("the requester cannot approve their own write-off")
        losses = await ledger.account_id(c, space_id, "ach_return_losses")
        deposit = await ledger.account_id(c, space_id, "customer_deposit", w["customer_id"])
        await ledger.post(
            c,
            space_id,
            [Entry("ach_debit.write_off", w["amount_minor"], losses, deposit, f"write_off:{w['id']}")],
        )
        cur = await c.execute(
            "UPDATE write_offs SET status = 'approved', approved_by = %s WHERE id = %s RETURNING *",
            (approved_by, w["id"]),
        )
        return await cur.fetchone()

    return await transaction(conn, work)
