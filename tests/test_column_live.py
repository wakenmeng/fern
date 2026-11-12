"""The five cases against the real Column sandbox, through the HTTP API.

Opt-in (network, creates sandbox objects):  FERN_RUN_COLUMN=1 uv run pytest tests/test_column_live.py -s
"""

import os

import httpx
import pytest
from conftest import TEST_DB

from fern.api import create_app
from fern.platform.config import Settings, get_settings

pytestmark = pytest.mark.skipif(os.environ.get("FERN_RUN_COLUMN") != "1", reason="set FERN_RUN_COLUMN=1")


@pytest.fixture
async def client():
    settings = Settings(
        env="test",
        database_url=TEST_DB,
        session_secret="test",
        bank="column",
        column_api_key=get_settings().column_api_key,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
            c.app = app
            yield c


async def test_five_cases_on_column_sandbox(client):
    s = (await client.post("/spaces")).json()
    h = {"Authorization": f"Bearer {s['token']}"}

    def idem(k):
        return {**h, "Idempotency-Key": k}

    async def my_available():
        me = (await client.get("/me", headers=h)).json()
        return next(c for c in me["customers"] if c["id"] == s["customer_id"])["available_minor"]

    # S1: payroll lands (Column simulation, force-settled)
    r = await client.post("/demo/payroll", json={"amount_minor": 2000_00}, headers=h)
    assert r.status_code == 201, r.text
    assert await my_available() == 2000_00

    # S2: pull 500, restricted until settled and released
    r = await client.post("/pulls", json={"amount_minor": 500_00}, headers=idem("pull-1"))
    assert r.status_code == 201, r.text
    assert await my_available() == 2000_00
    await client.post("/demo/settle", headers=h)
    await client.post("/demo/release-funds", headers=h)
    assert await my_available() == 2500_00

    # S3: P2P
    r = await client.post(
        "/p2p", json={"amount_minor": 50_00, "to_customer_id": s["npc_customer_id"]}, headers=idem("p2p-1")
    )
    assert r.status_code == 201

    # S4 + drill: Column creates the withdrawal but we lose the response
    r = await client.post("/withdrawals", json={"amount_minor": 300_00, "drill": "drop_response"}, headers=idem("wd-1"))
    wd = r.json()["id"]
    assert (await client.get(f"/transfers/{wd}", headers=h)).json()["transfer"]["status"] == "unknown"
    assert (await client.post("/transfers/recover-unknown", headers=h)).json()["retried"] == [wd]
    view = (await client.get(f"/transfers/{wd}", headers=h)).json()
    assert view["transfer"]["status"] == "processing"
    assert [j["effect"] for j in view["journals"]] == ["ach_credit.accept", "ach_credit.cash_out"]
    assert await my_available() == 2150_00

    # S5 (Column variant): an unauthorized return arrives at settlement, while the money is still
    # restricted, so it costs nothing. (The negative-balance variant runs on the simulator.)
    r = await client.post(
        "/pulls", json={"amount_minor": 100_00, "counterparty_name": "RETURN_UNAUTH"}, headers=idem("pull-2")
    )
    pull2 = r.json()["id"]
    await client.post("/demo/settle", headers=h)
    view = (await client.get(f"/transfers/{pull2}", headers=h)).json()
    assert view["transfer"]["status"] == "returned"
    assert view["transfer"]["return_code"] == "R29"
    assert await my_available() == 2150_00

    # Reconciliation: our bank account in the ledger equals Column's balance for this space.
    tb = (await client.get("/ledger/trial-balance", headers=h)).json()
    assert tb["debits_equal_credits"] and tb["balances_match_postings"]
    ours = next(a["balance_minor"] for a in tb["accounts"] if a["role"] == "bank")
    pool = client.app.state.pool
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT bank_account_id FROM spaces WHERE id = %s", (s["space_id"],))
        ).fetchone()
    column = (await client.app.state.bank.get_bank_account(row["bank_account_id"]))["balances"]
    print("ledger bank:", ours, "column:", column)
    assert ours == column["available_amount"] + column["pending_amount"]
