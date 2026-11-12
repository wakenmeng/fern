"""Bank interface and a simulated bank that behaves like Column's ACH (DESIGN §5.1, §6).

What matters for the ledger is *when cash moves*, which differs by direction:
  * Outgoing ACH credit (S4 withdrawal): cash leaves our account at initiation.
  * Outgoing ACH debit (S2 pull): cash arrives at settlement.
  * Returns put cash back (credit) or take it back (debit).

Errors are classified so callers never guess whether the bank created a transfer:
  * BankRejected: the bank definitely did not create it. Safe to fail the transfer.
  * BankOutcomeUnknown: timeout, connection drop, 5xx. The transfer may exist. Retry with the
    same idempotency key; never release the customer's money on this error.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

Direction = Literal["credit", "debit"]
Fault = Literal["drop_response", "unavailable"]


class BankRejected(Exception):
    pass


class BankOutcomeUnknown(Exception):
    pass


class BankConflict(Exception):
    """The bank returned a different transfer than we asked for under this idempotency key.
    Column does not reject a reused key with a different body; it silently returns the original."""


@dataclass(frozen=True)
class BankTxn:
    """A posting on our bank account. amount_minor > 0 means money in."""

    provider_txn_id: str
    amount_minor: int
    kind: Literal["initiation", "settlement", "return", "incoming"]
    posted_at: datetime


@dataclass(frozen=True)
class TransferSnapshot:
    bank_transfer_id: str
    direction: Direction
    amount_minor: int
    status: str  # initiated | settled | returned
    transactions: tuple[BankTxn, ...] = ()
    return_code: str | None = None


@dataclass(frozen=True)
class BankEvent:
    event_id: str
    bank_transfer_id: str
    type: str


class Bank(Protocol):
    provider: str

    async def create_ach_transfer(
        self,
        *,
        idempotency_key: str,
        direction: Direction,
        amount_minor: int,
        counterparty_name: str,
        account_ref: str | None = None,
    ) -> TransferSnapshot: ...

    async def get_transfer(self, bank_transfer_id: str) -> TransferSnapshot: ...

    async def open_account(self, space_id: str) -> tuple[str | None, str | None]: ...

    # Sandbox controls, used by the demo clock and drills.
    async def settle(self, bank_transfer_id: str) -> TransferSnapshot: ...

    async def receive_payroll(
        self, amount_minor: int, account_ref: str | None = None, account_number_ref: str | None = None
    ) -> BankTxn: ...

    def inject_fault(self, idempotency_key: str, fault: Fault) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SimulatedBank:
    """In-memory, Column-like bank with fault injection for drills and tests."""

    provider: str = "sim"
    transfers: dict[str, TransferSnapshot] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    faults: dict[str, Fault] = field(default_factory=dict)
    events: list[BankEvent] = field(default_factory=list)

    def inject_fault(self, idempotency_key: str, fault: Fault) -> None:
        """Make the next create call with this key fail in the given way."""
        self.faults[idempotency_key] = fault

    async def create_ach_transfer(
        self,
        *,
        idempotency_key: str,
        direction: Direction,
        amount_minor: int,
        counterparty_name: str,
        account_ref: str | None = None,
    ) -> TransferSnapshot:
        fault = self.faults.pop(idempotency_key, None)
        if fault == "unavailable":
            # 503 before the request was processed. The caller cannot tell, so it's "unknown".
            raise BankOutcomeUnknown("bank unavailable (503)")
        if amount_minor <= 0:
            raise BankRejected("amount must be positive")

        if idempotency_key in self.by_key:
            snapshot = self.transfers[self.by_key[idempotency_key]]
        else:
            bank_id = f"ach_{uuid4().hex[:16]}"
            txns: tuple[BankTxn, ...] = ()
            if direction == "credit":  # Column: outgoing credits leave the account at initiation
                txns = (BankTxn(f"txn_{uuid4().hex[:16]}", -amount_minor, "initiation", _now()),)
            snapshot = TransferSnapshot(bank_id, direction, amount_minor, "initiated", txns)
            self.transfers[bank_id] = snapshot
            self.by_key[idempotency_key] = bank_id
            self._emit(bank_id, "ach.outgoing_transfer.initiated")

        if fault == "drop_response":
            # The bank created the transfer, but the response never reached us.
            raise BankOutcomeUnknown("timed out waiting for the bank")
        return snapshot

    async def get_transfer(self, bank_transfer_id: str) -> TransferSnapshot:
        return self.transfers[bank_transfer_id]

    # --- sandbox controls (what Column's simulation endpoints do) ---

    async def settle(self, bank_transfer_id: str) -> TransferSnapshot:
        s = self.transfers[bank_transfer_id]
        if s.status != "initiated":
            return s
        txns = s.transactions
        if s.direction == "debit":  # pulled money arrives at settlement
            txns += (BankTxn(f"txn_{uuid4().hex[:16]}", s.amount_minor, "settlement", _now()),)
        s = replace(s, status="settled", transactions=txns)
        self.transfers[bank_transfer_id] = s
        self._emit(bank_transfer_id, "ach.outgoing_transfer.settled")
        return s

    async def return_transfer(self, bank_transfer_id: str, return_code: str) -> TransferSnapshot:
        s = self.transfers[bank_transfer_id]
        if s.status == "initiated":
            s = await self.settle(bank_transfer_id)  # a return always follows settlement
        if s.status == "returned":
            return s
        amount = s.amount_minor if s.direction == "credit" else -s.amount_minor
        txns = s.transactions + (BankTxn(f"txn_{uuid4().hex[:16]}", amount, "return", _now()),)
        s = replace(s, status="returned", transactions=txns, return_code=return_code)
        self.transfers[bank_transfer_id] = s
        self._emit(bank_transfer_id, "ach.outgoing_transfer.returned")
        return s

    async def receive_payroll(
        self, amount_minor: int, account_ref: str | None = None, account_number_ref: str | None = None
    ) -> BankTxn:
        """An incoming ACH credit, e.g. payroll, already posted to the account."""
        return BankTxn(f"txn_{uuid4().hex[:16]}", amount_minor, "incoming", _now())

    async def open_account(self, space_id: str) -> tuple[str | None, str | None]:
        """The simulated bank keeps one shared book; nothing to open."""
        return None, None

    def _emit(self, bank_transfer_id: str, event_type: str) -> None:
        self.events.append(BankEvent(f"evt_{uuid4().hex[:16]}", bank_transfer_id, event_type))
