"""Traces, the Redis rate limit and the Plaid balance check, through the HTTP API."""

import httpx
import pytest
from conftest import TEST_DB

from fern import payments
from fern.api import create_app
from fern.bank import SimulatedBank
from fern.plaid import SimulatedPlaid
from fern.platform.config import Settings
from fern.platform.ratelimit import RateLimiter


@pytest.fixture
async def make_client():
    async def make(**overrides):
        settings = Settings(env="test", database_url=TEST_DB, session_secret="test", plaid="sim", **overrides)
        app = create_app(settings, bank=SimulatedBank())
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append((ctx, client))
        return client

    clients: list = []
    yield make
    for ctx, client in clients:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


async def new_space(c: httpx.AsyncClient) -> dict:
    s = (await c.post("/spaces")).json()
    s["h"] = {"Authorization": f"Bearer {s['token']}"}
    return s


async def test_withdrawal_drill_trace_shows_every_hop(make_client):
    c = await make_client()
    s = await new_space(c)
    await c.post("/demo/payroll", json={"amount_minor": 1000_00}, headers=s["h"])

    r = await c.post(
        "/withdrawals",
        json={"amount_minor": 100_00, "drill": "drop_response"},
        headers={**s["h"], "Idempotency-Key": "w-1"},
    )
    trace_id = r.headers["X-Trace-Id"]
    t = (await c.get(f"/traces/{trace_id}", headers=s["h"])).json()

    spans = t["spans"]
    names = [(sp["service"], sp["name"]) for sp in spans]
    assert names[0] == ("api", "POST /withdrawals")
    assert ("redis", "EVALSHA token_bucket") in names
    assert ("api", "db.tx request_withdrawal") in names
    assert ("postgres", "SELECT accounts FOR UPDATE") in names
    assert ("postgres", "INSERT holds") in names
    bank_call = next(sp for sp in spans if sp["service"] == "bank-sim" and sp["name"] == "POST /transfers/ach")
    assert bank_call["status"] == "error"
    assert bank_call["attrs"]["error.type"] == "BankOutcomeUnknown"
    assert bank_call["attrs"]["idempotency_key"]
    assert t["status"] == "error"
    # Every span hangs off the root, and children start no earlier than their parent.
    by_id = {sp["id"]: sp for sp in spans}
    for sp in spans[1:]:
        assert by_id[sp["parent_id"]]["start_us"] <= sp["start_us"]

    # Recovery confirms the transfer: the retry reuses the key and the bank answers this time.
    r = await c.post("/transfers/recover-unknown", headers=s["h"])
    t = (await c.get(f"/traces/{r.headers['X-Trace-Id']}", headers=s["h"])).json()
    retry = next(sp for sp in t["spans"] if sp["service"] == "bank-sim" and sp["name"] == "POST /transfers/ach")
    assert retry["status"] == "ok" and retry["attrs"]["idempotency_key"] == bank_call["attrs"]["idempotency_key"]
    assert any(sp["name"] == "INSERT journals" for sp in t["spans"])


async def test_traces_are_private_to_their_space_and_reads_are_not_traced(make_client):
    c = await make_client()
    a, b = await new_space(c), await new_space(c)
    r = await c.post("/demo/payroll", json={"amount_minor": 10_00}, headers=a["h"])
    trace_id = r.headers["X-Trace-Id"]

    assert (await c.get(f"/traces/{trace_id}", headers=a["h"])).status_code == 200
    assert (await c.get(f"/traces/{trace_id}", headers=b["h"])).status_code == 404
    assert trace_id in [t["trace_id"] for t in (await c.get("/traces", headers=a["h"])).json()["traces"]]
    assert "X-Trace-Id" not in (await c.get("/me", headers=a["h"])).headers


async def test_money_requests_are_rate_limited_per_space(make_client):
    c = await make_client(rate_limit_per_minute=3)
    a, b = await new_space(c), await new_space(c)
    await c.post("/demo/payroll", json={"amount_minor": 100_00}, headers=a["h"])

    codes = []
    for i in range(4):
        r = await c.post(
            "/p2p",
            json={"to_customer_id": a["npc_customer_id"], "amount_minor": 1_00},
            headers={**a["h"], "Idempotency-Key": f"p-{i}"},
        )
        codes.append(r.status_code)
    assert codes == [201, 201, 201, 429]
    assert r.headers["Retry-After"]
    # Another space has its own bucket.
    r = await c.post(
        "/p2p",
        json={"to_customer_id": b["npc_customer_id"], "amount_minor": 1_00},
        headers={**b["h"], "Idempotency-Key": "p-0"},
    )
    assert r.status_code == 422  # not limited; b simply has no money


async def test_limiter_lets_requests_through_when_redis_is_down():
    limiter = RateLimiter("redis://localhost:1/0", per_minute=1)
    try:
        assert await limiter.allow("s") and await limiter.allow("s")
    finally:
        await limiter.aclose()


async def test_pull_is_refused_when_the_linked_account_cannot_cover_it(db, space, bank):
    r = await payments.request_pull(
        db, space.space_id, "customer:x", "pull-1", customer_id=space.customer_id, amount_minor=500_00
    )
    tid = r.body["id"]
    t = await payments.submit_to_bank(db, bank, space.space_id, tid, SimulatedPlaid(available_minor=100_00))

    assert t["status"] == "failed"
    assert bank.transfers == {}  # never reached the bank
    view = await payments.transfer_view(db, space.space_id, tid)
    assert "source_check_failed" in [e["event"] for e in view["timeline"]]
    assert (await payments.balances(db, space.space_id, space.customer_id))["available_minor"] == 0
