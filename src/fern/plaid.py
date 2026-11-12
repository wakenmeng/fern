"""Plaid: the customer's linked external account (S2).

Before we pull money with an ACH debit, we ask Plaid for the source account's available balance.
A pull the account can't cover would come back days later as an R01 return, so it is refused up
front instead. If Plaid can't answer, the pull is refused too: nothing else depends on Plaid.

Sandbox only. Each demo space gets one sandbox Item on its first pull, a custom sandbox user whose
checking account has a known balance.
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import httpx

from fern.platform import tracing

# Plaid sandbox "custom user": a checking account with a balance large enough for the demo.
CUSTOM_USER = {
    "override_accounts": [
        {
            "type": "depository",
            "subtype": "checking",
            "starting_balance": 3240.18,
            "meta": {"name": "Fern demo checking", "mask": "4421"},
        }
    ]
}


class PlaidError(Exception):
    """Plaid didn't give us an answer we can act on."""


class SourceAccount(Protocol):
    async def link(self) -> str:
        """Create the space's linked account; returns the access token."""
        ...

    async def available_balance(self, access_token: str) -> int:
        """Available balance of the linked checking account, in cents."""
        ...


@dataclass
class SimulatedPlaid:
    available_minor: int = 3_240_18

    async def link(self) -> str:
        with tracing.span("plaid", "POST /sandbox/public_token/create", **{"plaid.mode": "simulator"}):
            return f"access-sim-{uuid4().hex[:12]}"

    async def available_balance(self, access_token: str) -> int:
        with tracing.span("plaid", "POST /accounts/balance/get", **{"plaid.mode": "simulator"}) as s:
            if s is not None:
                s.attrs["plaid.available_minor"] = self.available_minor
            return self.available_minor


class PlaidClient:
    def __init__(self, client_id: str, secret: str, env: str = "sandbox", timeout: float = 15.0) -> None:
        if env != "sandbox":
            raise PlaidError("Fern only talks to the Plaid sandbox")
        self._auth = {"client_id": client_id, "secret": secret}
        self._http = httpx.AsyncClient(base_url="https://sandbox.plaid.com", timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> tuple[dict[str, Any], tracing.Span | None]:
        # Credentials and access tokens never go into span attributes.
        with tracing.span("plaid", f"POST {path}", **{"http.url": f"https://sandbox.plaid.com{path}"}) as s:
            try:
                r = await self._http.post(path, json={**self._auth, **body})
            except httpx.HTTPError as e:
                raise PlaidError(f"{path}: {type(e).__name__}") from e
            if s is not None:
                s.attrs["http.status_code"] = r.status_code
            if r.status_code >= 400:
                code = r.json().get("error_code") if r.content else None
                raise PlaidError(f"{path}: HTTP {r.status_code} {code or ''}".strip())
            return r.json(), s

    async def link(self) -> str:
        public, _ = await self._post(
            "/sandbox/public_token/create",
            {
                "institution_id": "ins_109508",
                "initial_products": ["auth"],
                "options": {"override_username": "user_custom", "override_password": json.dumps(CUSTOM_USER)},
            },
        )
        exchanged, _ = await self._post("/item/public_token/exchange", {"public_token": public["public_token"]})
        return exchanged["access_token"]

    async def available_balance(self, access_token: str) -> int:
        data, s = await self._post("/accounts/balance/get", {"access_token": access_token})
        checking = [a for a in data.get("accounts", []) if a.get("subtype") == "checking"]
        if not checking:
            raise PlaidError("no checking account on the linked item")
        b = checking[0]["balances"]
        amount = b["available"] if b.get("available") is not None else b["current"]
        cents = round(amount * 100)
        if s is not None:
            s.attrs.update({"plaid.account_mask": checking[0].get("mask"), "plaid.available_minor": cents})
        return cents
