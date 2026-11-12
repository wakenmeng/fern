"""Common failure modes (DESIGN §8)."""

import asyncio
import random
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from conftest import assert_books_balance, pay_salary, principal

from fern import ledger, payments
from fern.platform.idempotency import IdempotencyKeyReused
from fern.spaces import create_space


async def withdraw(db, space, key, amount):
    return await payments.request_withdrawal(
        db, space.space_id, principal(space), key, customer_id=space.customer_id, amount_minor=amount
    )


@pytest.mark.parametrize("fault", ["drop_response", "unavailable"])
async def test_s4_drill_bank_outcome_unknown_is_recovered_without_paying_twice(db, bank, space, fault):
    await pay_salary(db, bank, space, 1000_00)
    tid = UUID((await withdraw(db, space, "wd-1", 300_00)).body["id"])
    bank.inject_fault(str(tid), fault)

    t = await payments.submit_to_bank(db, bank, space.space_id, tid)
    assert t["status"] == "unknown"
    b = await payments.balances(db, space.space_id, space.customer_id)
    assert b["held_minor"] == 300_00 and b["available_minor"] == 700_00  # money stays held

    assert await payments.recover_unknown(db, bank, space.space_id) == [tid]
    t = await payments.get_transfer(db, space.space_id, tid)
    assert t["status"] == "processing"
    assert len(bank.transfers) == 1  # the bank has exactly one transfer
    view = await payments.transfer_view(db, space.space_id, tid)
    assert [j["effect"] for j in view["journals"]] == ["ach_credit.accept", "ach_credit.cash_out"]
    assert "bank_outcome_unknown" in [e["event"] for e in view["timeline"]]
    books = await assert_books_balance(db, space.space_id)
    assert books[("bank", None)] == 700_00


async def test_duplicate_and_out_of_order_webhooks_post_once(db, bank, space):
    await pay_salary(db, bank, space, 1000_00)
    tid = UUID((await withdraw(db, space, "wd-1", 300_00)).body["id"])
    t = await payments.submit_to_bank(db, bank, space.space_id, tid)
    await bank.settle(t["bank_transfer_id"])

    storm = bank.events * 10
    random.Random(7).shuffle(storm)
    results = [await payments.handle_bank_event(db, bank, e) for e in storm]
    assert results.count("applied") == len(bank.events)

    view = await payments.transfer_view(db, space.space_id, tid)
    assert view["transfer"]["status"] == "settled"
    assert len(view["journals"]) == 2
    await assert_books_balance(db, space.space_id)


async def test_concurrent_withdrawals_never_overdraw(pool, db, bank, space):
    await pay_salary(db, bank, space, 100_00)

    async def attempt(i: int) -> int:
        async with pool.connection() as c:
            return (await withdraw(c, space, f"wd-{i}", 10_00)).status_code

    codes = await asyncio.gather(*(attempt(i) for i in range(50)))
    assert codes.count(201) == 10 and codes.count(422) == 40
    b = await payments.balances(db, space.space_id, space.customer_id)
    assert b["available_minor"] == 0 and b["held_minor"] == 100_00


async def test_idempotent_replay_and_key_reuse(db, bank, space):
    await pay_salary(db, bank, space, 1000_00)
    first = await withdraw(db, space, "same-key", 100_00)
    again = await withdraw(db, space, "same-key", 100_00)
    assert again == first
    with pytest.raises(IdempotencyKeyReused):
        await withdraw(db, space, "same-key", 200_00)
    assert (await payments.balances(db, space.space_id, space.customer_id))["held_minor"] == 100_00


async def test_insufficient_funds_is_stored_for_replay(db, bank, space):
    first = await withdraw(db, space, "too-much", 1_00)
    assert first.status_code == 422
    await pay_salary(db, bank, space, 1000_00)
    assert await withdraw(db, space, "too-much", 1_00) == first  # same key, same answer


async def test_pull_returned_while_restricted_costs_nothing(db, bank, space):
    """The restriction is why an early return doesn't create a loss."""
    r = await payments.request_pull(
        db, space.space_id, principal(space), "pull-1", customer_id=space.customer_id, amount_minor=500_00
    )
    tid = UUID(r.body["id"])
    t = await payments.submit_to_bank(db, bank, space.space_id, tid)
    snapshot = await bank.return_transfer(t["bank_transfer_id"], "R01")
    await payments.apply_snapshot(db, bank.provider, space.space_id, tid, snapshot)

    b = await payments.balances(db, space.space_id, space.customer_id)
    assert b == {"balance_minor": 0, "held_minor": 0, "restricted_minor": 0, "available_minor": 0}
    books = await assert_books_balance(db, space.space_id)
    assert books[("ach_return_losses", None)] == 0


async def test_write_off_needs_a_second_person(db, bank, space):
    await pay_salary(db, bank, space, 100_00)
    r = await payments.request_pull(
        db, space.space_id, principal(space), "pull-1", customer_id=space.customer_id, amount_minor=50_00
    )
    pull = await payments.submit_to_bank(db, bank, space.space_id, UUID(r.body["id"]))
    snapshot = await bank.settle(pull["bank_transfer_id"])
    await payments.apply_snapshot(db, bank.provider, space.space_id, pull["id"], snapshot)
    await payments.release_restrictions(db, space.space_id, datetime(9999, 1, 1, tzinfo=UTC))
    await payments.send_p2p(
        db,
        space.space_id,
        principal(space),
        "p2p",
        from_customer=space.customer_id,
        to_customer=space.npc_customer_id,
        amount_minor=150_00,
    )
    snapshot = await bank.return_transfer(pull["bank_transfer_id"], "R10")
    await payments.apply_snapshot(db, bank.provider, space.space_id, pull["id"], snapshot)

    w = await payments.request_write_off(db, space.space_id, space.customer_id, "ops:alice")
    with pytest.raises(payments.PaymentError):
        await payments.approve_write_off(db, space.space_id, w["id"], "ops:alice")
    await payments.approve_write_off(db, space.space_id, w["id"], "ops:bob")
    assert (await payments.balances(db, space.space_id, space.customer_id))["balance_minor"] == 0


async def test_ledger_rejects_cash_without_a_matching_bank_transaction(db, space):
    bank_acct = await ledger.account_id(db, space.space_id, "bank")
    deposit = await ledger.account_id(db, space.space_id, "customer_deposit", space.customer_id)
    with pytest.raises(ledger.LedgerError, match="bank_transaction_id is required"):
        async with db.transaction():
            await ledger.post(
                db, space.space_id, [ledger.Entry("ach_in.credit", 100, bank_acct, deposit, f"x:{uuid4()}")]
            )


async def test_ledger_rejects_wrong_accounts_and_other_spaces(db, space):
    other = await create_space(db)
    mine = await ledger.account_id(db, space.space_id, "customer_deposit", space.customer_id)
    theirs = await ledger.account_id(db, other.space_id, "customer_deposit", other.customer_id)
    with pytest.raises(ledger.LedgerError, match="not found in this space"):
        async with db.transaction():
            await ledger.post(db, space.space_id, [ledger.Entry("p2p.transfer", 100, theirs, mine, f"x:{uuid4()}")])
    pending = await ledger.account_id(db, space.space_id, "ach_credit_pending")
    with pytest.raises(ledger.LedgerError, match="must debit"):
        async with db.transaction():
            await ledger.post(db, space.space_id, [ledger.Entry("p2p.transfer", 100, pending, mine, f"x:{uuid4()}")])
