"""The five cases through the HTTP API, the way the web app will call them."""

import httpx
import pytest
from conftest import TEST_DB

from fern.api import create_app
from fern.platform.config import Settings


@pytest.fixture
async def client():
    app = create_app(Settings(env="test", database_url=TEST_DB, session_secret="test"))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_all_five_cases_over_http(client):
    r = await client.post("/spaces")
    assert r.status_code == 201
    s = r.json()
    h = {"Authorization": f"Bearer {s['token']}"}

    def me_available():
        return next(c for c in me["customers"] if c["id"] == s["customer_id"])["available_minor"]

    # S1
    assert (await client.post("/demo/payroll", json={"amount_minor": 2000_00}, headers=h)).status_code == 201
    # S2
    r = await client.post("/pulls", json={"amount_minor": 500_00}, headers={**h, "Idempotency-Key": "pull-1"})
    pull_id = r.json()["id"]
    await client.post("/demo/settle", headers=h)
    await client.post("/demo/release-funds", headers=h)
    me = (await client.get("/me", headers=h)).json()
    assert me_available() == 2500_00
    # S3
    r = await client.post(
        "/p2p",
        json={"amount_minor": 50_00, "to_customer_id": s["npc_customer_id"]},
        headers={**h, "Idempotency-Key": "p2p-1"},
    )
    assert r.status_code == 201
    # S4 with the drill: the bank accepts but the response is lost
    r = await client.post(
        "/withdrawals",
        json={"amount_minor": 2250_00, "drill": "drop_response"},
        headers={**h, "Idempotency-Key": "wd-1"},
    )
    wd_id = r.json()["id"]
    assert (await client.get(f"/transfers/{wd_id}", headers=h)).json()["transfer"]["status"] == "unknown"
    assert (await client.post("/transfers/recover-unknown", headers=h)).json()["retried"] == [wd_id]
    assert (await client.get(f"/transfers/{wd_id}", headers=h)).json()["transfer"]["status"] == "processing"
    # S5
    r = await client.post(f"/demo/transfers/{pull_id}/return", json={"return_code": "R10"}, headers=h)
    assert r.json()["transfer"]["status"] == "returned"
    me = (await client.get("/me", headers=h)).json()
    assert me_available() == -300_00
    w = (await client.post("/write-offs", headers=h)).json()
    assert (await client.post(f"/write-offs/{w['id']}/approve", headers=h)).json()["status"] == "approved"

    tb = (await client.get("/ledger/trial-balance", headers=h)).json()
    assert tb["debits_equal_credits"] and tb["balances_match_postings"]


async def test_requests_need_a_session(client):
    assert (await client.get("/me")).status_code == 401
    assert (await client.get("/me", headers={"Authorization": "Bearer forged"})).status_code == 401
