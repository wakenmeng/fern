"""Reconciliation, its drill, and the AI report, through the HTTP API on the simulator."""

import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import TEST_DB

from fern import ai
from fern.api import create_app
from fern.bank import SimulatedBank
from fern.platform.config import Settings


@pytest.fixture
async def client():
    app = create_app(Settings(env="test", database_url=TEST_DB, session_secret="test", plaid="sim"), SimulatedBank())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            c.app = app
            yield c


async def space(c) -> dict:
    s = (await c.post("/spaces")).json()
    s["h"] = {"Authorization": f"Bearer {s['token']}"}
    s["k"] = lambda key: {**s["h"], "Idempotency-Key": key}
    return s


def open_kinds(rec: dict) -> list[str]:
    return sorted(e["kind"] for e in rec["exceptions"] if e["status"] == "open")


async def test_clean_books_reconcile_with_no_exceptions(client):
    s = await space(client)
    await client.post("/demo/payroll", json={"amount_minor": 1000_00}, headers=s["h"])
    await client.post("/withdrawals", json={"amount_minor": 200_00}, headers=s["k"]("w1"))
    await client.post("/pulls", json={"amount_minor": 100_00}, headers=s["k"]("p1"))
    await client.post("/demo/settle", headers=s["h"])

    rec = (await client.post("/ops/reconciliation/run", headers=s["h"])).json()
    assert open_kinds(rec) == []
    assert rec["last_run"]["matched"] == rec["last_run"]["scanned"] == 3  # payroll, cash out, pull settled


async def test_lost_response_shows_in_flight_then_resolves_after_recovery(client):
    s = await space(client)
    await client.post("/demo/payroll", json={"amount_minor": 1000_00}, headers=s["h"])
    await client.post("/withdrawals", json={"amount_minor": 300_00, "drill": "drop_response"}, headers=s["k"]("w1"))

    rec = (await client.post("/ops/reconciliation/run", headers=s["h"])).json()
    flagged = [e for e in rec["exceptions"] if e["kind"] == "in_flight_unconfirmed"]
    assert len(flagged) == 1 and flagged[0]["amount_minor"] == -300_00
    assert rec["last_run"]["open_exceptions"] == 0  # expected while unknown: not a person's problem yet

    await client.post("/transfers/recover-unknown", headers=s["h"])
    rec = (await client.post("/ops/reconciliation/run", headers=s["h"])).json()
    assert [e["status"] for e in rec["exceptions"]] == ["resolved"]


async def test_unbooked_bank_debit_drill_is_caught_and_escalated(client):
    s = await space(client)
    await client.post("/demo/payroll", json={"amount_minor": 1000_00}, headers=s["h"])
    assert (await client.post("/ops/drills/unbooked-bank-debit", json={}, headers=s["h"])).status_code == 201

    rec = (await client.post("/ops/reconciliation/run", headers=s["h"])).json()
    assert open_kinds(rec) == ["missing_in_books"]
    exc = rec["exceptions"][0]
    assert exc["amount_minor"] == -25_00

    r = await client.post(f"/ops/reconciliation/exceptions/{exc['id']}/escalate", json={}, headers=s["h"])
    assert r.json()["status"] == "escalated"
    # A later run keeps it escalated: a person owns it now.
    rec = (await client.post("/ops/reconciliation/run", headers=s["h"])).json()
    assert [e["status"] for e in rec["exceptions"]] == ["escalated"]

    summary = (await client.get("/ops/summary", headers=s["h"])).json()
    assert summary["books_balance"] is True


async def test_report_without_api_key_is_rule_based(client, monkeypatch):
    monkeypatch.setenv("FERN_ANTHROPIC_API_KEY", "")
    s = await space(client)
    await client.post("/demo/payroll", json={"amount_minor": 1000_00}, headers=s["h"])
    r = (await client.post("/ops/reports", headers=s["h"])).json()
    assert r["source"] == "rules"
    assert r["content"]["headline"] == "All clear"
    assert (await client.get("/ops/reports/latest", headers=s["h"])).json()["id"] == r["id"]


class FakeOpenAI:
    """Stands in for AsyncOpenAI: records the request, returns a schema-valid report."""

    def __init__(self, status: str = "completed", refuse: bool = False):
        self.calls: list[dict] = []
        self.status, self.refuse = status, refuse
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        facts = json.loads(kw["input"].split("\n\n", 1)[1])
        returned = [t["id"] for t in facts["transfers"] if t["status"] == "returned"]
        report = {
            "headline": "One returned pull",
            "summary": "A pull came back.",
            "findings": [{"severity": "warning", "title": "Return", "detail": "R10", "transfer_ids": returned}],
            "actions": [{"owner": "ops", "action": "Contact the customer."}],
        }
        part = SimpleNamespace(type="refusal", refusal="no") if self.refuse else SimpleNamespace(type="output_text")
        return SimpleNamespace(
            model=f"{kw['model']}-2025-08-07",
            status=self.status,
            incomplete_details=SimpleNamespace(reason="max_output_tokens") if self.status == "incomplete" else None,
            usage=SimpleNamespace(input_tokens=2000, output_tokens=500),
            output=[SimpleNamespace(type="message", content=[part])],
            output_text="" if self.refuse else json.dumps(report),
        )


async def test_llm_report_cites_transfers_and_records_cost(client):
    s = await space(client)
    await client.post("/demo/payroll", json={"amount_minor": 1000_00}, headers=s["h"])
    pull = (await client.post("/pulls", json={"amount_minor": 100_00}, headers=s["k"]("p1"))).json()
    await client.post(f"/demo/transfers/{pull['id']}/return", json={"return_code": "R10"}, headers=s["h"])

    fake = FakeOpenAI()
    settings = ai.AISettings(openai_api_key="test", ai_daily_budget_usd=100)
    async with client.app.state.pool.connection() as db:
        r = await ai.generate(db, s["space_id"], client=fake, settings=settings)

    assert r["source"] == "openai" and r["status"] == "done"
    assert r["content"]["findings"][0]["transfer_ids"] == [pull["id"]]
    assert r["cost_micros"] == 2000 * 5 + 500 * 30  # gpt-5.5: $5 / $30 per million tokens
    call = fake.calls[0]
    assert call["model"] == "gpt-5.5" and call["max_output_tokens"] == ai.MAX_TOKENS
    assert call["text"]["format"]["type"] == "json_schema" and call["text"]["format"]["strict"] is True


async def test_budget_cap_and_refusal_fall_back_to_rules(client):
    s = await space(client)
    async with client.app.state.pool.connection() as db:
        broke = ai.AISettings(openai_api_key="test", ai_daily_budget_usd=0.0)
        r = await ai.generate(db, s["space_id"], client=FakeOpenAI(), settings=broke)
        assert r["source"] == "rules" and "budget" in r["content"]["summary"]

        rich = ai.AISettings(openai_api_key="test", ai_daily_budget_usd=100)
        r = await ai.generate(db, s["space_id"], client=FakeOpenAI(refuse=True), settings=rich)
        assert r["source"] == "rules" and "refused" in r["content"]["summary"]
        r = await ai.generate(db, s["space_id"], client=FakeOpenAI(status="incomplete"), settings=rich)
        assert r["source"] == "rules" and "max_output_tokens" in r["content"]["summary"]
        cur = await db.execute(
            "SELECT status, cost_micros FROM ai_reports WHERE source = 'openai' AND space_id = %s", (s["space_id"],)
        )
        assert [dict(x) for x in await cur.fetchall()] == [{"status": "failed", "cost_micros": 0}] * 2
