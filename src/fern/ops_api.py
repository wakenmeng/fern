"""Operations endpoints: summary, reconciliation, drills and the AI report (DESIGN §7, §9).

Scoped to the caller's demo space like every other route. Kept in its own router so the main
API module stays about the five customer cases.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from fern import ai, ledger, recon
from fern.platform import session as sessions
from fern.platform import tracing
from fern.platform.session import Session

router = APIRouter(prefix="/ops", tags=["ops"])


async def _conn(request: Request):
    async with request.app.state.pool.connection() as c:
        yield c


def _session(request: Request, authorization: Annotated[str | None, Header()] = None) -> Session:
    token = (authorization or "").removeprefix("Bearer ").strip()
    s = sessions.verify(token, request.app.state.settings.session_secret) if token else None
    if s is None:
        raise HTTPException(401, "missing or invalid session token")
    tracing.set_space(s.space_id)
    return s


Conn = Annotated[AsyncConnection, Depends(_conn)]
Auth = Annotated[Session, Depends(_session)]


class EscalateRequest(BaseModel):
    note: str = Field(default="Escalated to the bank with the transaction id", max_length=500)


class DrillRequest(BaseModel):
    amount_minor: int = Field(default=25_00, gt=0, le=1_000_00)


@router.get("/summary")
async def summary(db: Conn, auth: Auth) -> dict[str, Any]:
    """The four numbers at the top of the Operations page, plus the work queue."""
    tb = await ledger.trial_balance(db, auth.space_id)
    roles = {a["role"]: a["balance_minor"] for a in tb["accounts"] if a["customer_id"] is None}
    cur = await db.execute(
        "SELECT id, kind, status, amount_minor, return_code, created_at FROM transfers "
        "WHERE space_id = %s AND status IN ('unknown', 'manual_review', 'returned') ORDER BY created_at DESC",
        (auth.space_id,),
    )
    queue = await cur.fetchall()
    cur = await db.execute(
        "SELECT c.id, c.display_name, a.raw_sum_minor FROM customers c "
        "JOIN accounts a ON a.customer_id = c.id AND a.role = 'customer_deposit' WHERE c.space_id = %s",
        (auth.space_id,),
    )
    negatives = [
        {
            "customer_id": r["id"],
            "name": r["display_name"],
            "balance_minor": ledger.normalized("customer_deposit", r["raw_sum_minor"]),
        }
        for r in await cur.fetchall()
        if ledger.normalized("customer_deposit", r["raw_sum_minor"]) < 0
    ]
    cur = await db.execute("SELECT * FROM write_offs WHERE space_id = %s ORDER BY created_at DESC", (auth.space_id,))
    write_offs = await cur.fetchall()
    rec = await recon.latest(db, auth.space_id)
    last = rec["last_run"]
    return {
        "books_balance": tb["debits_equal_credits"] and tb["balances_match_postings"],
        "bank_minus_books": (last["bank_balance"] - last["book_balance"])
        if last and last["bank_balance"] is not None
        else None,
        "needs_a_person": len([t for t in queue if t["status"] in ("unknown", "manual_review")])
        + len([e for e in rec["exceptions"] if e["status"] == "open"])
        + len([w for w in write_offs if w["status"] == "pending"]),
        "in_flight_minor": roles.get("ach_debit_receivable", 0) + roles.get("ach_credit_pending", 0),
        "queue": queue,
        "negative_balances": negatives,
        "write_offs": write_offs,
        "reconciliation": rec,
    }


@router.post("/reconciliation/run")
async def run_reconciliation(request: Request, db: Conn, auth: Auth) -> dict[str, Any]:
    return await recon.run(db, request.app.state.bank, auth.space_id)


@router.get("/reconciliation")
async def reconciliation(db: Conn, auth: Auth) -> dict[str, Any]:
    return await recon.latest(db, auth.space_id)


@router.post("/reconciliation/exceptions/{exception_id}/escalate")
async def escalate(exception_id: UUID, body: EscalateRequest, db: Conn, auth: Auth) -> dict[str, Any]:
    try:
        return await recon.escalate(db, auth.space_id, exception_id, body.note)
    except LookupError:
        raise HTTPException(404, "no such exception") from None


@router.post("/drills/unbooked-bank-debit", status_code=201)
async def drill_unbooked(request: Request, body: DrillRequest, db: Conn, auth: Auth) -> dict[str, Any]:
    """Money leaves the bank account without the ledger knowing. Run reconciliation to catch it."""
    bank_id = await recon.drill_unbooked_bank_debit(db, request.app.state.bank, auth.space_id, body.amount_minor)
    return {"bank_transfer_id": bank_id, "next": "POST /ops/reconciliation/run"}


@router.post("/reports", status_code=201)
async def create_report(db: Conn, auth: Auth) -> dict[str, Any]:
    return await ai.generate(db, auth.space_id)


@router.get("/reports/latest")
async def latest_report(db: Conn, auth: Auth) -> dict[str, Any]:
    r = await ai.latest(db, auth.space_id)
    if r is None:
        raise HTTPException(404, "no report yet")
    return r
