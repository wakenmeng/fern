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

from fern.platform import tracing

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

    # Pre-funding the account pool: request a credit now, pick it up when it has landed.
    async def request_incoming_credit(self, amount_minor: int, account_ref: str, account_number_ref: str) -> None: ...

    async def find_incoming_credit(self, account_ref: str, exclude: frozenset[str] = frozenset()) -> BankTxn | None: ...

    def inject_fault(self, idempotency_key: str, fault: Fault) -> None: ...


def _sim(**attrs: object) -> dict[str, object]:
    return {"bank.mode": "in-process simulator (Column-like)", **attrs}


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
    incoming: dict[str, list[BankTxn]] = field(default_factory=dict)

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
        with tracing.span("bank-sim", "POST /transfers/ach", **_sim(idempotency_key=idempotency_key)) as s:
            return self._create(s, idempotency_key, direction, amount_minor)

    def _create(
        self, s: tracing.Span | None, idempotency_key: str, direction: Direction, amount_minor: int
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

        if s is not None:
            s.attrs.update({"bank.transfer_id": snapshot.bank_transfer_id, "bank.status": snapshot.status})
        if fault == "drop_response":
            # The bank created the transfer, but the response never reached us.
            if s is not None:
                s.attrs["drill"] = "the bank created the transfer; the response is dropped"
            raise BankOutcomeUnknown("timed out waiting for the bank")
        return snapshot

    async def get_transfer(self, bank_transfer_id: str) -> TransferSnapshot:
        with tracing.span("bank-sim", "GET /transfers/ach/{id}", **_sim()) as s:
            snapshot = self.transfers[bank_transfer_id]
            if s is not None:
                s.attrs.update({"bank.transfer_id": bank_transfer_id, "bank.status": snapshot.status})
            return snapshot

    # --- sandbox controls (what Column's simulation endpoints do) ---

    async def settle(self, bank_transfer_id: str) -> TransferSnapshot:
        with tracing.span("bank-sim", "POST /simulate/transfers/ach/settle", **_sim()):
            return self._settle(bank_transfer_id)

    def _settle(self, bank_transfer_id: str) -> TransferSnapshot:
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
        with tracing.span("bank-sim", "POST /simulate/transfers/ach/return", **_sim(return_code=return_code)):
            return self._return(bank_transfer_id, return_code)

    def _return(self, bank_transfer_id: str, return_code: str) -> TransferSnapshot:
        s = self.transfers[bank_transfer_id]
        if s.status == "initiated":
            s = self._settle(bank_transfer_id)  # a return always follows settlement
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
        with tracing.span("bank-sim", "POST /simulate/receive-ach-credit", **_sim(amount=amount_minor)):
            return BankTxn(f"txn_{uuid4().hex[:16]}", amount_minor, "incoming", _now())

    async def request_incoming_credit(self, amount_minor: int, account_ref: str, account_number_ref: str) -> None:
        self.incoming.setdefault(account_ref, []).append(await self.receive_payroll(amount_minor))

    async def find_incoming_credit(self, account_ref: str, exclude: frozenset[str] = frozenset()) -> BankTxn | None:
        return next((t for t in self.incoming.get(account_ref, []) if t.provider_txn_id not in exclude), None)

    async def open_account(self, space_id: str) -> tuple[str | None, str | None]:
        return f"sim_acct_{space_id}", f"sim_num_{space_id}"

    def _emit(self, bank_transfer_id: str, event_type: str) -> None:
        self.events.append(BankEvent(f"evt_{uuid4().hex[:16]}", bank_transfer_id, event_type))
