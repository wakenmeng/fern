"""Column sandbox adapter (DESIGN §6.4). Same interface as SimulatedBank.

Cash moments come from the Column ACH transfer object itself:
  * outgoing CREDIT: money leaves at `initiated_at`
  * outgoing DEBIT:  money arrives at `settled_at`
  * either:          a return moves it back at `returned_at`
Each becomes a BankTxn with a stable id ("<transfer id>:<kind>"), so re-reading the same
transfer never produces a new cash movement.

Error mapping: timeouts, connection errors, 429 and 5xx are BankOutcomeUnknown (Column may
have created the transfer); other 4xx are BankRejected (it did not).

Verified against the sandbox on 2026-11-12 (scripts/column_probe.py):
  * pulled debit sits in pending_amount, becomes available at settlement
  * pushed credit leaves available_amount immediately at creation
  * same Idempotency-Key with a different body returns the ORIGINAL transfer, no error
    -> we compare and raise BankConflict
  * RETURN_UNAUTH (R29) arrives at the moment of settlement (settled_at == returned_at)
  * simulated incoming ACH credits wait for the next Fed window (SCHEDULED) unless force-settled
"""

import asyncio
import json as _json
import re
from datetime import datetime
from typing import Any

import httpx

from fern.bank import BankConflict, BankOutcomeUnknown, BankRejected, BankTxn, Direction, Fault, TransferSnapshot
from fern.platform import tracing

# Column statuses -> our three coarse states. Anything not listed is unexpected and is raised.
_IN_FLIGHT = {
    "PRE_REVIEW",
    "INITIATED",
    "HOLD",
    "PENDING_SUBMISSION",
    "SUBMITTED",
    "SCHEDULED",
    "MANUAL_REVIEW",
    "MANUAL_REVIEW_APPROVED",
}
_SETTLED = {"SETTLED", "COMPLETED", "RETURN_DISHONORED", "RETURN_DISHONORED_FUNDS_UNLOCKED"}
_RETURNED = {"RETURNED", "RETURN_CONTESTED"}

# A valid ABA routing number for the sandbox counterparty (the external account).
SANDBOX_COUNTERPARTY = {"routing_number": "021000021", "account_number": "000123456789", "account_type": "checking"}


def _route(path: str) -> str:
    """Span name for a path: ids replaced by placeholders so similar calls group together."""
    return re.sub(r"/[a-z]+_[A-Za-z0-9]{8,}", "/{id}", path.split("?")[0])


class ColumnError(Exception):
    pass


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def to_snapshot(obj: dict[str, Any]) -> TransferSnapshot:
    status = obj["status"]
    if status in _IN_FLIGHT:
        coarse = "initiated"
    elif status in _SETTLED:
        coarse = "settled"
    elif status in _RETURNED:
        coarse = "returned"
    elif status == "CANCELED":
        coarse = "canceled"
    else:
        raise ColumnError(f"unexpected Column ACH status {status}")

    tid, amount = obj["id"], int(obj["amount"])
    direction: Direction = "credit" if obj["type"] == "CREDIT" else "debit"
    initiated = _ts(obj.get("initiated_at")) or _ts(obj["created_at"])
    settled, returned = _ts(obj.get("settled_at")), _ts(obj.get("returned_at"))

    txns: list[BankTxn] = []
    if direction == "credit":
        txns.append(BankTxn(f"{tid}:initiation", -amount, "initiation", initiated))
        if coarse in ("returned", "canceled"):  # either way the money comes back to the account
            back_at = returned or _ts(obj.get("cancelled_at")) or initiated
            txns.append(BankTxn(f"{tid}:return", amount, "return", back_at))
    else:
        if settled:
            txns.append(BankTxn(f"{tid}:settlement", amount, "settlement", settled))
        if coarse == "returned" and settled:
            txns.append(BankTxn(f"{tid}:return", -amount, "return", returned or settled))

    details = obj.get("return_details") or []
    return_code = details[-1].get("return_code") if details else None
    return TransferSnapshot(tid, direction, amount, coarse, tuple(txns), return_code)


class ColumnBank:
    provider = "column"

    def __init__(self, api_key: str, base_url: str = "https://api.column.com", timeout: float = 15.0) -> None:
        if not api_key.startswith("test_"):
            raise ColumnError("refusing to run without a Column sandbox key (test_...)")
        self.base_url = base_url
        self._http = httpx.AsyncClient(base_url=base_url, auth=("", api_key), timeout=timeout)
        self._faults: dict[str, Fault] = {}
        self._entity_id: str | None = None

    def inject_fault(self, idempotency_key: str, fault: Fault) -> None:
        """Drill (DESIGN §8.1): make the next create with this key fail like a flaky network would."""
        self._faults[idempotency_key] = fault

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        drop_response: bool = False,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        attrs: dict[str, Any] = {"http.method": method, "http.url": f"{self.base_url}{path}", "peer.service": "Column"}
        if idempotency_key:
            attrs["idempotency_key"] = idempotency_key
        if json is not None:
            attrs["http.request.body"] = _json.dumps(json)[:600]
        if params:
            attrs["http.request.params"] = _json.dumps(params)
        with tracing.span("column", f"{method} {_route(path)}", **attrs) as s:
            for attempt in range(5):
                try:
                    r = await self._http.request(method, path, json=json, params=params, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    raise BankOutcomeUnknown(f"{method} {path}: {type(e).__name__}") from e
                # Rate limited: the request was not processed, so retrying (same idempotency key) is safe.
                if r.status_code != 429 or attempt == 4:
                    break
                if s is not None:
                    s.attrs["http.retries_after_429"] = attempt + 1
                await asyncio.sleep(float(r.headers.get("Retry-After") or 2**attempt))
            if s is not None:
                s.attrs["http.status_code"] = r.status_code
            if r.status_code == 429 or r.status_code >= 500:
                raise BankOutcomeUnknown(f"{method} {path}: HTTP {r.status_code}")
            if r.status_code >= 400:
                raise BankRejected(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
            body = r.json() if r.content else {}
            if s is not None and isinstance(body, dict):
                s.attrs.update({f"column.{k}": body[k] for k in ("id", "status", "type", "amount") if k in body})
            if drop_response:
                if s is not None:
                    s.attrs["drill"] = (
                        "Column created the transfer; its reply is dropped as if the connection timed out"
                    )
                raise BankOutcomeUnknown("injected: Column created the transfer but the response was lost")
            return body

    # --- Bank interface

    async def create_ach_transfer(
        self,
        *,
        idempotency_key: str,
        direction: Direction,
        amount_minor: int,
        counterparty_name: str,
        account_ref: str | None = None,
    ) -> TransferSnapshot:
        if account_ref is None:
            raise BankRejected("a Column bank account id is required")
        fault = self._faults.pop(idempotency_key, None)
        if fault == "unavailable":
            with tracing.span("column", "POST /transfers/ach", drill="503 before the request reached Column"):
                raise BankOutcomeUnknown("injected: 503 before the request reached Column")
        body = {
            "type": "CREDIT" if direction == "credit" else "DEBIT",
            # Consumer accounts: PPD for credits we push, WEB for debits authorized online.
            "entry_class_code": "PPD" if direction == "credit" else "WEB",
            "amount": amount_minor,
            "currency_code": "USD",
            "bank_account_id": account_ref,
            "counterparty": {**SANDBOX_COUNTERPARTY, "name": counterparty_name[:127]},
            # The sandbox triggers returns from magic receiver names such as RETURN_UNAUTH.
            "receiver_name": counterparty_name[:22],
            "description": f"fern {idempotency_key}",
        }
        obj = await self._request(
            "POST", "/transfers/ach", idempotency_key=idempotency_key, json=body, drop_response=fault == "drop_response"
        )
        if int(obj["amount"]) != amount_minor or obj["type"] != body["type"]:
            raise BankConflict(f"key {idempotency_key} already used for {obj['id']} ({obj['type']} {obj['amount']})")
        return to_snapshot(obj)

    async def get_transfer(self, bank_transfer_id: str) -> TransferSnapshot:
        return to_snapshot(await self.get_transfer_raw(bank_transfer_id))

    # --- sandbox controls and setup

    async def get_transfer_raw(self, bank_transfer_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/transfers/ach/{bank_transfer_id}")

    async def settle(self, bank_transfer_id: str) -> TransferSnapshot:
        await self._request("POST", "/simulate/transfers/ach/settle", json={"ach_transfer_id": bank_transfer_id})
        return await self.get_transfer(bank_transfer_id)

    async def request_incoming_credit(self, amount_minor: int, account_ref: str, account_number_ref: str) -> None:
        """Ask the sandbox to send us an incoming ACH credit (payroll). It shows up ~2 minutes later."""
        await self._request(
            "POST",
            "/simulate/receive-ach-credit",
            json={"amount": amount_minor, "currency_code": "USD", "destination_account_number_id": account_number_ref},
        )

    async def find_incoming_credit(self, account_ref: str, exclude: frozenset[str] = frozenset()) -> BankTxn | None:
        """The first incoming credit on the account not in `exclude`, force-settled (otherwise it waits
        for the next Fed window), as a cash transaction keyed by Column's transfer id."""
        new = [t for t in await self._incoming_credits(account_ref) if t["id"] not in exclude]
        if not new:
            return None
        t = new[0]
        if t["status"] not in _SETTLED:
            await self._request("POST", "/simulate/transfers/ach/settle", json={"ach_transfer_id": t["id"]})
            t = await self.get_transfer_raw(t["id"])
        return BankTxn(t["id"], int(t["amount"]), "incoming", _ts(t.get("settled_at")) or _ts(t["created_at"]))

    async def receive_payroll(
        self, amount_minor: int, account_ref: str | None = None, account_number_ref: str | None = None
    ) -> BankTxn:
        """Request an incoming credit and wait for it. Slow on the sandbox (~2 minutes), which is why
        the live demo hands out pre-funded spaces from the pool (fern.pool) instead."""
        if not account_ref or not account_number_ref:
            raise BankRejected("this space has no Column bank account")
        before = frozenset(t["id"] for t in await self._incoming_credits(account_ref))
        await self.request_incoming_credit(amount_minor, account_ref, account_number_ref)
        for _ in range(48):
            if txn := await self.find_incoming_credit(account_ref, before):
                return txn
            await asyncio.sleep(5)
        raise BankOutcomeUnknown("simulated payroll did not appear on the account within 4 minutes")

    async def _incoming_credits(self, bank_account_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", "/transfers/ach", params={"bank_account_id": bank_account_id, "limit": 100})
        return [t for t in data.get("transfers", []) if t.get("is_incoming") and t.get("type") == "CREDIT"]

    async def open_account(self, space_id: str) -> tuple[str | None, str | None]:
        """One Column bank account per demo space, under a single platform entity (DESIGN §2.1)."""
        if self._entity_id is None:
            entity = await self.create_platform_entity("Fern Demo Platform", "fern-platform-entity-v1")
            self._entity_id = entity["id"]
        account = await self.create_bank_account(self._entity_id, f"fern space {space_id}", f"fern-space-{space_id}")
        return account["id"], account["default_account_number_id"]

    async def get_bank_account(self, bank_account_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/bank-accounts/{bank_account_id}")

    async def create_platform_entity(self, name: str, idempotency_key: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/entities/business",
            idempotency_key=idempotency_key,
            json={
                "business_name": name,
                "ein_pending": False,
                "ein": "123456789",
                "address": {
                    "line_1": "555 California Street",
                    "city": "San Francisco",
                    "state": "CA",
                    "postal_code": "94104",
                    "country_code": "US",
                },
            },
        )

    async def create_bank_account(self, entity_id: str, description: str, idempotency_key: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/bank-accounts",
            idempotency_key=idempotency_key,
            json={"entity_id": entity_id, "description": description},
        )
