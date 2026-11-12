"""The pre-funded account pool: provisioning, claiming under concurrency, and S1 from a pooled space."""

import asyncio

import httpx
import pytest
from conftest import TEST_DB, assert_books_balance

from fern import pool as fern_pool
from fern.api import create_app
from fern.bank import SimulatedBank
from fern.platform.config import Settings


async def drain(db) -> None:
    await db.execute("UPDATE spaces SET pool_status = 'claimed' WHERE pool_status <> 'claimed'")


async def test_provision_then_concurrent_claims_never_share_a_space(db, pool, bank):
    await drain(db)
    result = await fern_pool.provision(db, bank, 3, poll_seconds=0, log=lambda _: None)
    assert result["ready"] == 3 and result["provisioning"] == 0
    # Re-running tops up to the target size; it doesn't open more.
    assert (await fern_pool.provision(db, bank, 3, poll_seconds=0, log=lambda _: None))["ready"] == 3

    async def one_claim():
        async with pool.connection() as c:
            return await fern_pool.claim(c)

    claimed = await asyncio.gather(*(one_claim() for _ in range(8)))
    got = [s.space_id for s in claimed if s]
    assert len(got) == len(set(got)) == 3
    assert (await fern_pool.stats(db))["ready"] == 0


@pytest.fixture
async def pooled_client(db):
    await drain(db)
    bank = SimulatedBank()
    app = create_app(Settings(env="test", database_url=TEST_DB, session_secret="test", pool_only=True), bank)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            await fern_pool.provision(db, bank, 1, poll_seconds=0, log=lambda _: None)
            yield c


async def test_pooled_space_payroll_is_already_at_the_bank(pooled_client, db):
    c = pooled_client
    s = (await c.post("/spaces")).json()
    h = {"Authorization": f"Bearer {s['token']}"}

    # The requested amount is ignored: the pooled payroll ($2,000) already landed.
    first = (await c.post("/demo/payroll", json={"amount_minor": 1}, headers=h)).json()
    again = (await c.post("/demo/payroll", headers=h)).json()
    assert first["id"] == again["id"] and first["amount_minor"] == fern_pool.PAYROLL_MINOR
    me = (await c.get("/me", headers=h)).json()
    assert next(x for x in me["customers"] if x["id"] == s["customer_id"])["available_minor"] == 2000_00
    await assert_books_balance(db, s["space_id"])

    # Pool empty and pool_only: the landing page shows "full" instead of a slow space.
    r = await c.post("/spaces")
    assert r.status_code == 503
    assert (await c.get("/pool")).json()["ready"] == 0
