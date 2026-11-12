"""Double-entry ledger (DESIGN §4).

Every accounting effect is one journal with two postings: +amount on the debit account,
-amount on the credit account. Each effect has exactly one legal shape (EFFECTS below), and
any effect that moves cash must point at a bank transaction with the matching amount.

`post` is idempotent per dedupe_key: posting the same effect twice is a no-op, posting a
different effect under the same key raises LedgerConflict. Callers run it inside their own
database transaction.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

from psycopg import AsyncConnection

from fern.platform import tracing

# Normal side of each account role. Customer deposits are liabilities (credit-normal).
NORMAL_SIDE = {
    "bank": "D",
    "ach_debit_receivable": "D",
    "customer_deposit": "C",
    "ach_credit_pending": "C",
    "ach_return_losses": "D",
}
SYSTEM_ROLES = [role for role in NORMAL_SIDE if role != "customer_deposit"]


@dataclass(frozen=True)
class Effect:
    debit_role: str
    credit_role: str
    may_go_negative: bool = False  # only returns may push a customer below zero


EFFECTS = {
    # S1: payroll arrives.
    "ach_in.credit": Effect("bank", "customer_deposit"),
    # S2 / S5: we pull money from an external account.
    "ach_debit.accept": Effect("ach_debit_receivable", "customer_deposit"),
    "ach_debit.settle": Effect("bank", "ach_debit_receivable"),
    "ach_debit.return": Effect("customer_deposit", "bank", may_go_negative=True),
    "ach_debit.accept_reversal": Effect("customer_deposit", "ach_debit_receivable"),  # returned before settling
    "ach_debit.write_off": Effect("ach_return_losses", "customer_deposit"),
    # S3
    "p2p.transfer": Effect("customer_deposit", "customer_deposit"),
    # S4: we push money to an external account.
    "ach_credit.accept": Effect("customer_deposit", "ach_credit_pending"),
    "ach_credit.cash_out": Effect("ach_credit_pending", "bank"),
    "ach_credit.return": Effect("bank", "customer_deposit"),
}


class LedgerError(Exception):
    pass


class LedgerConflict(LedgerError):
    """A dedupe_key was reused for a different effect. Should never happen; alert on it."""


@dataclass(frozen=True)
class Entry:
    effect: str
    amount_minor: int
    debit_account_id: UUID
    credit_account_id: UUID
    dedupe_key: str
    transfer_id: UUID | None = None
    bank_transaction_id: UUID | None = None


def normalized(role: str, raw_sum_minor: int) -> int:
    """Balance as a positive number on the account's normal side."""
    return raw_sum_minor if NORMAL_SIDE[role] == "D" else -raw_sum_minor


async def lock_accounts(conn: AsyncConnection, space_id: UUID, account_ids: list[UUID]) -> dict[UUID, dict]:
    """Lock account rows in id order (a fixed order avoids deadlocks) and return them."""
    ids = sorted(set(account_ids))
    cur = await conn.execute(
        "SELECT id, role, customer_id, raw_sum_minor FROM accounts "
        "WHERE id = ANY(%s) AND space_id = %s ORDER BY id FOR UPDATE",
        (ids, space_id),
    )
    rows = {row["id"]: row for row in await cur.fetchall()}
    if len(rows) != len(ids):
        raise LedgerError("account not found in this space")
    return rows


async def post(conn: AsyncConnection, space_id: UUID, entries: list[Entry]) -> None:
    new: list[Entry] = []
    for e in entries:
        cur = await conn.execute(
            "SELECT effect, amount_minor, debit_account_id, credit_account_id, bank_transaction_id "
            "FROM journals WHERE dedupe_key = %s",
            (e.dedupe_key,),
        )
        existing = await cur.fetchone()
        if existing is None:
            new.append(e)
        elif (
            existing["effect"],
            existing["amount_minor"],
            existing["debit_account_id"],
            existing["credit_account_id"],
            existing["bank_transaction_id"],
        ) != (e.effect, e.amount_minor, e.debit_account_id, e.credit_account_id, e.bank_transaction_id):
            raise LedgerConflict(f"dedupe_key {e.dedupe_key} already used with different content")
    if not new:
        return

    accounts = await lock_accounts(conn, space_id, [a for e in new for a in (e.debit_account_id, e.credit_account_id)])
    for e in new:
        debit, credit = accounts.get(e.debit_account_id, {}), accounts.get(e.credit_account_id, {})
        attrs = {
            "ledger.effect": e.effect,
            "ledger.amount_minor": e.amount_minor,
            "ledger.debit": debit.get("role"),
            "ledger.credit": credit.get("role"),
            "ledger.dedupe_key": e.dedupe_key,
        }
        # Customer accounts share a role; the id tells the UI whose deposit it is.
        for side, acct in (("debit", debit), ("credit", credit)):
            if acct.get("customer_id"):
                attrs[f"ledger.{side}_customer_id"] = str(acct["customer_id"])
        with tracing.span(None, f"ledger.post {e.effect}", **attrs):
            await _post_one(conn, space_id, e, accounts)


async def _post_one(conn: AsyncConnection, space_id: UUID, e: Entry, accounts: dict[UUID, dict]) -> None:
    rule = EFFECTS.get(e.effect)
    if rule is None:
        raise LedgerError(f"unknown effect {e.effect}")
    if e.amount_minor <= 0:
        raise LedgerError("amount must be positive")
    debit, credit = accounts[e.debit_account_id], accounts[e.credit_account_id]
    if (debit["role"], credit["role"]) != (rule.debit_role, rule.credit_role):
        raise LedgerError(
            f"{e.effect} must debit {rule.debit_role} and credit {rule.credit_role}, "
            f"got {debit['role']} / {credit['role']}"
        )

    # Cash only moves with a bank transaction of the same amount and direction behind it.
    touches_bank = "bank" in (rule.debit_role, rule.credit_role)
    if touches_bank != (e.bank_transaction_id is not None):
        raise LedgerError(f"{e.effect}: bank_transaction_id is {'required' if touches_bank else 'not allowed'}")
    if touches_bank:
        cur = await conn.execute(
            "SELECT amount_minor FROM bank_transactions WHERE id = %s AND space_id = %s",
            (e.bank_transaction_id, space_id),
        )
        txn = await cur.fetchone()
        expected = e.amount_minor if rule.debit_role == "bank" else -e.amount_minor
        if txn is None or txn["amount_minor"] != expected:
            raise LedgerError(f"{e.effect}: bank transaction does not match")

    journal_id = uuid4()
    await conn.execute(
        "INSERT INTO journals (id, space_id, effect, amount_minor, debit_account_id, credit_account_id, "
        "transfer_id, bank_transaction_id, dedupe_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            journal_id,
            space_id,
            e.effect,
            e.amount_minor,
            e.debit_account_id,
            e.credit_account_id,
            e.transfer_id,
            e.bank_transaction_id,
            e.dedupe_key,
        ),
    )
    for account, amount in ((debit, e.amount_minor), (credit, -e.amount_minor)):
        await conn.execute(
            "INSERT INTO postings (journal_id, account_id, amount_minor) VALUES (%s, %s, %s)",
            (journal_id, account["id"], amount),
        )
        await conn.execute(
            "UPDATE accounts SET raw_sum_minor = raw_sum_minor + %s WHERE id = %s", (amount, account["id"])
        )
        account["raw_sum_minor"] += amount

    if debit["role"] == "customer_deposit" and not rule.may_go_negative:
        if normalized("customer_deposit", debit["raw_sum_minor"]) < 0:
            raise LedgerError(f"{e.effect} would make the customer balance negative")
    if e.effect == "ach_debit.write_off" and normalized("customer_deposit", credit["raw_sum_minor"]) > 0:
        raise LedgerError("write-off is larger than the customer's negative balance")


async def account_id(conn: AsyncConnection, space_id: UUID, role: str, customer_id: UUID | None = None) -> UUID:
    cur = await conn.execute(
        "SELECT id FROM accounts WHERE space_id = %s AND role = %s AND customer_id IS NOT DISTINCT FROM %s",
        (space_id, role, customer_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise LedgerError(f"no {role} account in this space")
    return row["id"]


async def trial_balance(conn: AsyncConnection, space_id: UUID) -> dict:
    """Per-account balances plus the two checks that must always hold."""
    cur = await conn.execute(
        """
        SELECT a.id, a.role, a.customer_id, a.raw_sum_minor,
               coalesce((SELECT sum(p.amount_minor) FROM postings p WHERE p.account_id = a.id), 0)::bigint
                   AS postings_sum
        FROM accounts a WHERE a.space_id = %s ORDER BY a.role, a.customer_id NULLS FIRST
        """,
        (space_id,),
    )
    rows = await cur.fetchall()
    return {
        "accounts": [
            {
                "account_id": str(r["id"]),
                "role": r["role"],
                "customer_id": str(r["customer_id"]) if r["customer_id"] else None,
                "balance_minor": normalized(r["role"], r["raw_sum_minor"]),
            }
            for r in rows
        ],
        "debits_equal_credits": sum(r["raw_sum_minor"] for r in rows) == 0,
        "balances_match_postings": all(r["raw_sum_minor"] == r["postings_sum"] for r in rows),
    }
