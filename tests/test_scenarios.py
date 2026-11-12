"""The five cases, end to end against the simulated bank (DESIGN §1, §5.3)."""

from datetime import UTC, datetime
from uuid import UUID

from conftest import assert_books_balance, pay_salary, principal

from fern import payments

FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)


async def balance(db, space, customer=None):
    return await payments.balances(db, space.space_id, customer or space.customer_id)


async def test_s1_payroll_lands_in_the_account(db, bank, space):
    tid = await pay_salary(db, bank, space, 2000_00)

    assert (await balance(db, space))["available_minor"] == 2000_00
    view = await payments.transfer_view(db, space.space_id, tid)
    assert [j["effect"] for j in view["journals"]] == ["ach_in.credit"]
    books = await assert_books_balance(db, space.space_id)
    assert books[("bank", None)] == 2000_00


async def test_s2_pulled_money_is_restricted_until_settled_and_released(db, bank, space):
    r = await payments.request_pull(
        db, space.space_id, principal(space), "pull-1", customer_id=space.customer_id, amount_minor=500_00
    )
    tid = UUID(r.body["id"])
    t = await payments.submit_to_bank(db, bank, space.space_id, tid)
    assert t["status"] == "processing"
    b = await balance(db, space)
    assert (b["balance_minor"], b["available_minor"]) == (500_00, 0)

    snapshot = await bank.settle(t["bank_transfer_id"])
    await payments.apply_snapshot(db, bank.provider, space.space_id, tid, snapshot)
    assert (await balance(db, space))["available_minor"] == 0  # settled, but still in the hold period

    assert await payments.release_restrictions(db, space.space_id, FAR_FUTURE) == 1
    assert (await balance(db, space))["available_minor"] == 500_00
    books = await assert_books_balance(db, space.space_id)
    assert books[("bank", None)] == 500_00
    assert books[("ach_debit_receivable", None)] == 0


async def test_s3_p2p_moves_money_instantly(db, bank, space):
    await pay_salary(db, bank, space, 2000_00)
    r = await payments.send_p2p(
        db,
        space.space_id,
        principal(space),
        "p2p-1",
        from_customer=space.customer_id,
        to_customer=space.npc_customer_id,
        amount_minor=50_00,
    )
    assert r.status_code == 201
    assert (await balance(db, space))["available_minor"] == 1950_00
    assert (await balance(db, space, space.npc_customer_id))["available_minor"] == 50_00
    await assert_books_balance(db, space.space_id)


async def test_s4_withdrawal_holds_then_cash_leaves(db, bank, space):
    await pay_salary(db, bank, space, 2000_00)
    r = await payments.request_withdrawal(
        db, space.space_id, principal(space), "wd-1", customer_id=space.customer_id, amount_minor=300_00
    )
    tid = UUID(r.body["id"])
    assert (await balance(db, space))["available_minor"] == 1700_00  # held before the bank call

    t = await payments.submit_to_bank(db, bank, space.space_id, tid)
    assert t["status"] == "processing"
    b = await balance(db, space)
    assert (b["balance_minor"], b["held_minor"], b["available_minor"]) == (1700_00, 0, 1700_00)

    snapshot = await bank.settle(t["bank_transfer_id"])
    await payments.apply_snapshot(db, bank.provider, space.space_id, tid, snapshot)
    view = await payments.transfer_view(db, space.space_id, tid)
    assert view["transfer"]["status"] == "settled"
    assert [j["effect"] for j in view["journals"]] == ["ach_credit.accept", "ach_credit.cash_out"]
    books = await assert_books_balance(db, space.space_id)
    assert books[("bank", None)] == 1700_00
    assert books[("ach_credit_pending", None)] == 0


async def test_s5_returned_pull_after_release_goes_negative_and_is_written_off(db, bank, space):
    """DESIGN §5.3: 2000 payroll, 500 pull released, 2300 withdrawn, then the pull is returned."""
    await pay_salary(db, bank, space, 2000_00)
    r = await payments.request_pull(
        db, space.space_id, principal(space), "pull-1", customer_id=space.customer_id, amount_minor=500_00
    )
    pull_id = UUID(r.body["id"])
    pull = await payments.submit_to_bank(db, bank, space.space_id, pull_id)
    await payments.apply_snapshot(
        db, bank.provider, space.space_id, pull_id, await bank.settle(pull["bank_transfer_id"])
    )
    await payments.release_restrictions(db, space.space_id, FAR_FUTURE)

    r = await payments.request_withdrawal(
        db, space.space_id, principal(space), "wd-1", customer_id=space.customer_id, amount_minor=2300_00
    )
    await payments.submit_to_bank(db, bank, space.space_id, UUID(r.body["id"]))
    assert (await balance(db, space))["balance_minor"] == 200_00

    snapshot = await bank.return_transfer(pull["bank_transfer_id"], "R10")
    await payments.apply_snapshot(db, bank.provider, space.space_id, pull_id, snapshot)
    b = await balance(db, space)
    assert (b["balance_minor"], b["available_minor"]) == (-300_00, -300_00)

    # Frozen: nothing can be spent while the balance is negative.
    r = await payments.request_withdrawal(
        db, space.space_id, principal(space), "wd-2", customer_id=space.customer_id, amount_minor=1
    )
    assert r.status_code == 422

    w = await payments.request_write_off(db, space.space_id, space.customer_id, "ops:alice")
    assert w["amount_minor"] == 300_00
    await payments.approve_write_off(db, space.space_id, w["id"], "ops:bob")
    assert (await balance(db, space))["balance_minor"] == 0

    books = await assert_books_balance(db, space.space_id)
    assert books[("ach_return_losses", None)] == 300_00
    # Every bank transaction posted exactly once: +2000, +500, -2300, -500.
    assert books[("bank", None)] == -300_00
    view = await payments.transfer_view(db, space.space_id, pull_id)
    assert view["transfer"]["return_code"] == "R10"
    assert "account_negative" in [e["event"] for e in view["timeline"]]
