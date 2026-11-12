"""AI operations report for a space (DESIGN §9).

One Claude call, not an agent: we gather the facts with read-only queries (books, transfers,
reconciliation, write-offs), hand them over as JSON, and ask for a structured report that cites
transfer ids. The model never gets a tool that can write, so it cannot change the books.

Spend is capped per day in Postgres: each report reserves its worst-case cost before calling
Claude (under an advisory lock) and records the actual cost from `usage` afterwards. Without an
API key, over budget, or if Claude declines, a rule-based report is produced instead and labelled
as such.
"""

import json
from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from fern import ledger, recon
from fern.platform import tracing


class AISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FERN_", env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5-5"
    ai_daily_budget_usd: float = 10.0


MAX_TOKENS = 8000
# USD per million tokens (input, output). Unknown models are priced as the default one.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-opus-4-8": (5.0, 25.0), "claude-sonnet-5-5": (2.0, 10.0)}

SYSTEM = """You write the operations report for one demo space of Fern, a neobank backend running \
against bank sandboxes (no real money). Readers are the ops and engineering on-call.

Use only the facts in the JSON you are given. Cite every transfer you mention by its id in \
transfer_ids. Amounts in the facts are minor units (cents); write them as dollars. Say plainly when \
something is a drill or comes from the simulator. If the books balance and nothing is open, say so \
in one finding instead of inventing problems. Keep each detail to two or three sentences."""


class Finding(BaseModel):
    severity: str = Field(pattern="^(info|warning|critical)$")
    title: str
    detail: str
    transfer_ids: list[str]


class Action(BaseModel):
    owner: str = Field(pattern="^(ops|engineering|risk)$")
    action: str


class Report(BaseModel):
    headline: str
    summary: str
    findings: list[Finding]
    actions: list[Action]


SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "transfer_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["severity", "title", "detail", "transfer_ids"],
                "additionalProperties": False,
            },
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "enum": ["ops", "engineering", "risk"]},
                    "action": {"type": "string"},
                },
                "required": ["owner", "action"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "summary", "findings", "actions"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------ facts (read only)


async def gather_facts(conn: AsyncConnection, space_id: UUID) -> dict[str, Any]:
    with tracing.span("api", "gather facts (read-only queries)"):
        tb = await ledger.trial_balance(conn, space_id)
        cur = await conn.execute(
            "SELECT t.id::text, t.kind, t.status, t.amount_minor, t.return_code, t.failure_reason, "
            "t.counterparty_name, t.created_at::text, "
            "(SELECT array_agg(e.event ORDER BY e.id) FROM transfer_events e WHERE e.transfer_id = t.id) "
            "AS events FROM transfers t WHERE t.space_id = %s ORDER BY t.created_at",
            (space_id,),
        )
        transfers = await cur.fetchall()
        cur = await conn.execute(
            "SELECT c.display_name, c.kind, a.raw_sum_minor FROM customers c "
            "JOIN accounts a ON a.customer_id = c.id AND a.role = 'customer_deposit' WHERE c.space_id = %s",
            (space_id,),
        )
        customers = [
            {
                "name": r["display_name"],
                "kind": r["kind"],
                "balance_minor": ledger.normalized("customer_deposit", r["raw_sum_minor"]),
            }
            for r in await cur.fetchall()
        ]
        cur = await conn.execute(
            "SELECT id::text, amount_minor, status, requested_by, approved_by FROM write_offs WHERE space_id = %s",
            (space_id,),
        )
        write_offs = await cur.fetchall()
        rec = await recon.latest(conn, space_id)
    return {
        "books": {
            "debits_equal_credits": tb["debits_equal_credits"],
            "balances_match_postings": tb["balances_match_postings"],
            "accounts": [
                {"role": a["role"], "balance_minor": a["balance_minor"]}
                for a in tb["accounts"]
                if a["customer_id"] is None
            ],
        },
        "customers": customers,
        "transfers": transfers,
        "write_offs": write_offs,
        "reconciliation": {
            "last_run": _jsonable(rec["last_run"]),
            "exceptions": [_jsonable(e) for e in rec["exceptions"]],
        },
    }


def _jsonable(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return json.loads(json.dumps(row, default=str)) if row is not None else None


# ------------------------------------------------------------------ the rule-based fallback


def rules_report(facts: dict[str, Any], reason: str) -> Report:
    findings: list[Finding] = []
    books = facts["books"]
    if not (books["debits_equal_credits"] and books["balances_match_postings"]):
        findings.append(
            Finding(
                severity="critical",
                title="Books out of balance",
                detail="The trial balance failed. Stop money movement and investigate.",
                transfer_ids=[],
            )
        )
    for c in facts["customers"]:
        if c["balance_minor"] < 0:
            findings.append(
                Finding(
                    severity="warning",
                    title=f"{c['name']} is negative",
                    detail=f"Balance ${c['balance_minor'] / 100:,.2f}; sending and withdrawing are frozen.",
                    transfer_ids=[t["id"] for t in facts["transfers"] if t["status"] == "returned"],
                )
            )
    for t in facts["transfers"]:
        if t["status"] in ("unknown", "manual_review"):
            findings.append(
                Finding(
                    severity="warning",
                    title=f"Transfer {t['status'].replace('_', ' ')}",
                    detail=f"{t['kind']} of ${t['amount_minor'] / 100:,.2f} needs confirmation.",
                    transfer_ids=[t["id"]],
                )
            )
        elif t["status"] == "returned":
            findings.append(
                Finding(
                    severity="info",
                    title=f"Returned {t['kind']} ({t['return_code']})",
                    detail=f"${t['amount_minor'] / 100:,.2f} came back.",
                    transfer_ids=[t["id"]],
                )
            )
    for e in facts["reconciliation"]["exceptions"]:
        if e["status"] != "resolved":
            findings.append(
                Finding(
                    severity="warning",
                    title=f"Reconciliation: {e['kind'].replace('_', ' ')}",
                    detail=e["detail"],
                    transfer_ids=[],
                )
            )
    if not findings:
        findings.append(
            Finding(
                severity="info",
                title="All clear",
                detail="Books balance, nothing is in an unknown state, reconciliation is clean.",
                transfer_ids=[],
            )
        )
    worst = (
        "critical"
        if any(f.severity == "critical" for f in findings)
        else "warning"
        if any(f.severity == "warning" for f in findings)
        else "info"
    )
    return Report(
        headline={"critical": "Books need attention now", "warning": "Items need follow-up", "info": "All clear"}[
            worst
        ],
        summary=f"Rule-based report ({reason}).",
        findings=findings,
        actions=[Action(owner="ops", action="Work through the open findings above.")] if worst != "info" else [],
    )


# ------------------------------------------------------------------ budget


def _price(model: str) -> tuple[float, float]:
    return PRICES.get(model, PRICES["claude-opus-5-5"])


def worst_case_micros(model: str, facts_json: str, system: str = SYSTEM) -> int:
    pin, pout = _price(model)
    est_in = (len(facts_json) + len(system)) // 3 + 500  # generous chars-per-token estimate
    return int(est_in * pin + MAX_TOKENS * pout)  # $/MTok * tokens = micro-dollars


async def _reserve(conn: AsyncConnection, space_id: UUID, model: str, micros: int, cap_micros: int) -> UUID | None:
    """Reserve worst-case spend for today; None if it would go over the daily cap."""
    report_id = uuid4()
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('fern.ai_budget'))")
        cur = await conn.execute(
            "SELECT coalesce(sum(coalesce(cost_micros, reserved_micros)), 0)::bigint AS spent FROM ai_reports "
            "WHERE source = 'claude' AND created_at >= date_trunc('day', now())"
        )
        spent = (await cur.fetchone())["spent"]
        if spent + micros > cap_micros:
            return None
        await conn.execute(
            "INSERT INTO ai_reports (id, space_id, status, source, model, reserved_micros) "
            "VALUES (%s, %s, 'running', 'claude', %s, %s)",
            (report_id, space_id, model, micros),
        )
    return report_id


async def spent_today(conn: AsyncConnection) -> int:
    cur = await conn.execute(
        "SELECT coalesce(sum(coalesce(cost_micros, reserved_micros)), 0)::bigint AS spent FROM ai_reports "
        "WHERE source = 'claude' AND created_at >= date_trunc('day', now())"
    )
    return (await cur.fetchone())["spent"]


# ------------------------------------------------------------------ the report


async def _ask_claude(client: Any, model: str, facts_json: str) -> tuple[Report, Any]:
    with tracing.span("claude", "POST /v1/messages", **{"llm.model": model, "llm.max_tokens": MAX_TOKENS}) as s:
        response = await client.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            # If a safety classifier declines, the API re-runs the request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": f"Facts for today's report:\n\n{facts_json}"}],
        )
        if s is not None:
            s.attrs.update(
                {
                    "llm.served_by": response.model,
                    "llm.stop_reason": response.stop_reason,
                    "tokens.in": response.usage.input_tokens,
                    "tokens.out": response.usage.output_tokens,
                }
            )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write the report")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the report hit max_tokens")
    text = next(b.text for b in response.content if b.type == "text")
    return Report.model_validate_json(text), response


async def generate(
    conn: AsyncConnection, space_id: UUID, *, client: Any = None, settings: AISettings | None = None
) -> dict[str, Any]:
    settings = settings or AISettings()
    facts = await gather_facts(conn, space_id)
    facts_json = json.dumps(facts, default=str, sort_keys=True)

    async def save_rules(reason: str) -> dict[str, Any]:
        report = rules_report(facts, reason)
        rid = uuid4()
        await conn.execute(
            "INSERT INTO ai_reports (id, space_id, status, source, content) VALUES (%s, %s, 'done', 'rules', %s)",
            (rid, space_id, Jsonb(report.model_dump())),
        )
        return await get(conn, space_id, rid)

    if client is None:
        if not settings.anthropic_api_key:
            return await save_rules("no Anthropic API key configured")
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    model = settings.ai_model
    reserve = worst_case_micros(model, facts_json)
    with tracing.span(
        "postgres", "reserve AI budget", **{"reserved_usd": reserve / 1e6, "cap_usd": settings.ai_daily_budget_usd}
    ):
        report_id = await _reserve(conn, space_id, model, reserve, int(settings.ai_daily_budget_usd * 1e6))
    if report_id is None:
        return await save_rules("today's AI budget is used up")

    try:
        report, response = await _ask_claude(client, model, facts_json)
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the rules report
        await conn.execute(
            "UPDATE ai_reports SET status = 'failed', cost_micros = 0, error = %s WHERE id = %s",
            (f"{type(exc).__name__}: {exc}"[:500], report_id),
        )
        return await save_rules(f"Claude unavailable: {type(exc).__name__}")

    pin, pout = _price(response.model)
    cost = int(response.usage.input_tokens * pin + response.usage.output_tokens * pout)
    await conn.execute(
        "UPDATE ai_reports SET status = 'done', model = %s, cost_micros = %s, tokens_in = %s, tokens_out = %s, "
        "content = %s WHERE id = %s",
        (
            response.model,
            cost,
            response.usage.input_tokens,
            response.usage.output_tokens,
            Jsonb(report.model_dump()),
            report_id,
        ),
    )
    return await get(conn, space_id, report_id)


async def get(conn: AsyncConnection, space_id: UUID, report_id: UUID) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT id, created_at, status, source, model, cost_micros, tokens_in, tokens_out, content "
        "FROM ai_reports WHERE id = %s AND space_id = %s",
        (report_id, space_id),
    )
    return await cur.fetchone()


async def latest(conn: AsyncConnection, space_id: UUID) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT id FROM ai_reports WHERE space_id = %s AND status = 'done' ORDER BY created_at DESC LIMIT 1",
        (space_id,),
    )
    row = await cur.fetchone()
    return await get(conn, space_id, row["id"]) if row else None
