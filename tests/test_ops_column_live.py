"""Reconciliation against the real Column sandbox. Opt-in, like test_column_live.py:

FERN_RUN_COLUMN=1 uv run pytest tests/test_ops_column_live.py -s
"""

import os

import httpx
import pytest
from conftest import TEST_DB

from fern import pool as fern_pool
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
        plaid="sim",
        column_api_key=get_settings().column_api_key,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
            c.app = app
            yield c


async def test_reconciliation_on_column(client):
    async with client.app.state.pool.connection() as conn:
        await conn.execute("UPDATE spaces SET pool_status = 'claimed' WHERE pool_status <> 'claimed'")
        assert (await fern_pool.provision(conn, client.app.state.bank, 1, poll_seconds=10))["ready"] == 1
    s = (await client.post("/spaces")).json()
    h = {"Authorization": f"Bearer {s['token']}"}

    # Before S1 the payroll is already at Column but not in our books: expected, not an exception.
    rec = (await client.post("/ops/reconciliation/run", headers=h)).json()
    print("before S1:", rec["last_run"])
    assert [e for e in rec["exceptions"] if e["status"] == "open"] == []

    await client.post("/demo/payroll", headers=h)
    await client.post("/withdrawals", json={"amount_minor": 300_00}, headers={**h, "Idempotency-Key": "w1"})
    await client.post("/pulls", json={"amount_minor": 100_00}, headers={**h, "Idempotency-Key": "p1"})
    rec = (await client.post("/ops/reconciliation/run", headers=h)).json()
    print("pull in flight:", rec["last_run"], rec["exceptions"])
    assert [e for e in rec["exceptions"] if e["status"] == "open"] == []

    await client.post("/demo/settle", headers=h)
    rec = (await client.post("/ops/reconciliation/run", headers=h)).json()
    print("settled:", rec["last_run"])
    assert rec["last_run"]["bank_balance"] == rec["last_run"]["book_balance"] == 1800_00
    assert [e for e in rec["exceptions"] if e["status"] == "open"] == []

    # Drill: $25 leaves the Column account behind the ledger's back.
    assert (await client.post("/ops/drills/unbooked-bank-debit", json={}, headers=h)).status_code == 201
    rec = (await client.post("/ops/reconciliation/run", headers=h)).json()
    print("after drill:", rec["last_run"], [(e["kind"], e["amount_minor"]) for e in rec["exceptions"]])
    kinds = sorted(e["kind"] for e in rec["exceptions"] if e["status"] == "open")
    assert kinds == ["balance_mismatch", "missing_in_books"]
