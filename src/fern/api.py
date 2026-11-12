"""HTTP API for the five cases. Run: uvicorn fern.api:app

Every request is authenticated with the session token returned by POST /spaces, which pins it
to one demo space. Money-moving POSTs require an Idempotency-Key header.

The /demo routes drive the bank's sandbox controls: payroll, settlement, returns. FERN_BANK
selects the in-process simulator ("sim") or the Column sandbox ("column").
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from fern import ledger, payments, pool, spaces
from fern.bank import Bank, BankEvent, BankTxn, SimulatedBank
from fern.column import ColumnBank
from fern.plaid import PlaidClient, SimulatedPlaid, SourceAccount
from fern.platform import session as sessions
from fern.platform import tracing
from fern.platform.config import Settings, get_settings
from fern.platform.db import create_pool
from fern.platform.idempotency import IdempotencyKeyReused, Response
from fern.platform.ratelimit import RateLimiter
from fern.platform.session import Session

SIMULATED_APPROVER = "ops:simulated-approver"


def make_bank(settings: Settings) -> Bank:
    if settings.bank == "column":
        return ColumnBank(settings.column_api_key, settings.column_base_url)
    return SimulatedBank()


def make_source(settings: Settings) -> SourceAccount:
    if settings.plaid == "sandbox":
        return PlaidClient(settings.plaid_client_id, settings.plaid_secret, settings.plaid_env)
    return SimulatedPlaid()


def create_app(settings: Settings | None = None, bank: Bank | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = create_pool(settings.database_url)
        await app.state.pool.open()
        app.state.limiter = (
            RateLimiter(settings.redis_url, settings.rate_limit_per_minute) if settings.redis_url else None
        )
        yield
        await app.state.pool.close()
        for client in (app.state.bank, app.state.source, app.state.limiter):
            if hasattr(client, "aclose"):
                await client.aclose()

    app = FastAPI(title="Fern", version="0.1.0", root_path=settings.root_path, lifespan=lifespan)
    app.state.settings = settings
    app.state.bank = bank or make_bank(settings)
    app.state.source = make_source(settings)

    @app.middleware("http")
    async def trace_requests(request: Request, call_next):
        # State-changing requests get a trace, stored under the caller's space (FRONTEND §3).
        path = request.url.path
        if request.method == "GET" or "/traces" in path:
            return await call_next(request)
        attrs: dict[str, Any] = {"http.method": request.method, "http.target": path}
        for header, attr in (("cf-ray", "cf.ray"), ("x-amzn-trace-id", "aws.alb.trace_id")):
            if value := request.headers.get(header):
                attrs[attr] = value
        with tracing.start("api", f"{request.method} {path}", **attrs) as tr:
            response = await call_next(request)
            if (route := request.scope.get("route")) is not None:
                tr.root.name = f"{request.method} {route.path}"
            tr.root.attrs["http.status_code"] = response.status_code
            if response.status_code >= 500:
                tr.root.status = "error"
        response.headers["X-Trace-Id"] = tr.id
        if tr.space_id is not None:
            async with app.state.pool.connection() as c:
                await tracing.save(c, tr)
        return response

    @app.exception_handler(payments.PaymentError)
    async def payment_error(_: Request, exc: payments.PaymentError) -> JSONResponse:
        return JSONResponse({"error": type(exc).__name__, "message": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(IdempotencyKeyReused)
    async def key_reused(_: Request, exc: IdempotencyKeyReused) -> JSONResponse:
        return JSONResponse(
            {"error": "IdempotencyKeyReused", "message": "this key was used with a different request"},
            status_code=422,
        )

    _routes(app)
    return app


# --------------------------------------------------------------------------- dependencies


async def conn(request: Request):
    async with request.app.state.pool.connection() as c:
        yield c


def current_session(request: Request, authorization: Annotated[str | None, Header()] = None) -> Session:
    token = (authorization or "").removeprefix("Bearer ").strip()
    s = sessions.verify(token, request.app.state.settings.session_secret) if token else None
    if s is None:
        raise HTTPException(401, "missing or invalid session token")
    tracing.set_space(s.space_id)
    return s


async def rate_limited(request: Request, auth: Annotated[Session, Depends(current_session)]) -> None:
    """Money-moving requests: a token bucket per space in Redis."""
    limiter: RateLimiter | None = request.app.state.limiter
    if limiter is not None and not await limiter.allow(str(auth.space_id)):
        raise HTTPException(429, "too many requests for this space", headers={"Retry-After": "2"})


Conn = Annotated[AsyncConnection, Depends(conn)]
Auth = Annotated[Session, Depends(current_session)]
IdemKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
Limited = Depends(rate_limited)


def _json(r: Response) -> JSONResponse:
    return JSONResponse(r.body, status_code=r.status_code)


class Amount(BaseModel):
    amount_minor: int = Field(gt=0, le=1_000_000_00)


class PayrollRequest(BaseModel):
    # Ignored for pooled spaces, whose payroll has already landed at the bank.
    amount_minor: int = Field(default=pool.PAYROLL_MINOR, gt=0, le=1_000_000_00)


class P2PRequest(Amount):
    to_customer_id: UUID


class PullRequest(Amount):
    # Column's sandbox returns a debit when the receiver name is a magic value like RETURN_UNAUTH.
    counterparty_name: str = Field(default="External checking", max_length=80)


class WithdrawalRequest(Amount):
    drill: Literal["drop_response", "unavailable"] | None = None


class ReturnRequest(BaseModel):
    return_code: str = Field(default="R10", pattern=r"^R\d\d$")


# --------------------------------------------------------------------------- routes


def _routes(app: FastAPI) -> None:
    bank: Bank = app.state.bank

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/pool")
    async def pool_stats(db: Conn) -> dict[str, int]:
        return await pool.stats(db)

    def _session_body(s: spaces.NewSpace) -> dict[str, Any]:
        tracing.set_space(s.space_id)
        token = sessions.issue(Session(s.space_id, s.customer_id), app.state.settings.session_secret)
        return {
            "space_id": s.space_id,
            "customer_id": s.customer_id,
            "npc_customer_id": s.npc_customer_id,
            "token": token,
        }

    @app.get("/spaces")
    async def list_spaces(db: Conn) -> dict[str, Any]:
        """Landing page: how many new spaces are left, and existing ones a visitor can join."""
        return {"ready": (await pool.stats(db))["ready"], "spaces": await pool.existing(db)}

    @app.post("/spaces", status_code=201)
    async def create_space(db: Conn) -> dict[str, Any]:
        s = await pool.claim(db)
        if s is None:
            if app.state.settings.pool_only:
                raise HTTPException(503, "no new demo spaces left; join an existing one from GET /spaces")
            s = await spaces.create_space(db, bank)
        return _session_body(s)

    @app.post("/spaces/{space_id}/join")
    async def join_space(space_id: UUID, db: Conn) -> dict[str, Any]:
        s = await pool.join(db, space_id)
        if s is None:
            raise HTTPException(404, "no such space")
        return _session_body(s)

    @app.get("/me")
    async def me(db: Conn, auth: Auth) -> dict[str, Any]:
        cur = await db.execute(
            "SELECT id, kind, display_name FROM customers WHERE space_id = %s ORDER BY kind DESC",
            (auth.space_id,),
        )
        people = []
        for c in await cur.fetchall():
            people.append({**c, **await payments.balances(db, auth.space_id, c["id"])})
        return {"space_id": auth.space_id, "customer_id": auth.customer_id, "customers": people}

    # S1
    @app.post("/demo/payroll", status_code=201)
    async def payroll(db: Conn, auth: Auth, body: PayrollRequest | None = None) -> dict[str, Any]:
        cur = await db.execute("SELECT * FROM spaces WHERE id = %s", (auth.space_id,))
        sp = await cur.fetchone()
        if sp["payroll_txn_id"]:  # pooled space: the credit is already on the bank account
            txn = BankTxn(sp["payroll_txn_id"], sp["payroll_amount_minor"], "incoming", sp["payroll_posted_at"])
        else:
            amount = (body or PayrollRequest()).amount_minor
            txn = await bank.receive_payroll(amount, sp["bank_account_id"], sp["bank_account_number_id"])
        tid = await payments.record_incoming_credit(db, bank.provider, auth.space_id, auth.customer_id, txn)
        return (await payments.transfer_view(db, auth.space_id, tid))["transfer"]

    # S2 / S5
    @app.post("/pulls", dependencies=[Limited])
    async def pull(body: PullRequest, db: Conn, auth: Auth, key: IdemKey) -> JSONResponse:
        r = await payments.request_pull(
            db,
            auth.space_id,
            auth.principal,
            key,
            customer_id=auth.customer_id,
            amount_minor=body.amount_minor,
            counterparty_name=body.counterparty_name,
        )
        if r.status_code == 201:
            await payments.submit_to_bank(db, bank, auth.space_id, UUID(r.body["id"]), app.state.source)
        return _json(r)

    # S3
    @app.post("/p2p", dependencies=[Limited])
    async def p2p(body: P2PRequest, db: Conn, auth: Auth, key: IdemKey) -> JSONResponse:
        return _json(
            await payments.send_p2p(
                db,
                auth.space_id,
                auth.principal,
                key,
                from_customer=auth.customer_id,
                to_customer=body.to_customer_id,
                amount_minor=body.amount_minor,
            )
        )

    # S4
    @app.post("/withdrawals", dependencies=[Limited])
    async def withdraw(body: WithdrawalRequest, db: Conn, auth: Auth, key: IdemKey) -> JSONResponse:
        r = await payments.request_withdrawal(
            db,
            auth.space_id,
            auth.principal,
            key,
            customer_id=auth.customer_id,
            amount_minor=body.amount_minor,
        )
        if r.status_code == 201:
            tid = UUID(r.body["id"])
            if body.drill:
                bank.inject_fault(str(tid), body.drill)
            await payments.submit_to_bank(db, bank, auth.space_id, tid)
        return _json(r)

    @app.get("/transfers/{transfer_id}")
    async def transfer(transfer_id: UUID, db: Conn, auth: Auth) -> dict[str, Any]:
        return await payments.transfer_view(db, auth.space_id, transfer_id)

    @app.post("/transfers/recover-unknown")
    async def recover(db: Conn, auth: Auth) -> dict[str, Any]:
        return {"retried": await payments.recover_unknown(db, bank, auth.space_id)}

    # Demo clock and sandbox controls
    @app.post("/demo/settle")
    async def settle_all(db: Conn, auth: Auth) -> dict[str, Any]:
        cur = await db.execute(
            "SELECT id, bank_transfer_id FROM transfers WHERE space_id = %s AND status = 'processing'",
            (auth.space_id,),
        )
        settled = []
        for t in await cur.fetchall():
            snapshot = await bank.settle(t["bank_transfer_id"])
            await payments.apply_snapshot(db, bank.provider, auth.space_id, t["id"], snapshot)
            settled.append(t["id"])
        return {"settled": settled}

    @app.post("/demo/release-funds")
    async def release(db: Conn, auth: Auth) -> dict[str, Any]:
        # Demo clock: skip the hold period, but only for money that has already settled.
        far_future = datetime(9999, 1, 1, tzinfo=UTC)
        return {"released": await payments.release_restrictions(db, auth.space_id, far_future)}

    @app.post("/demo/transfers/{transfer_id}/return")
    async def return_transfer(transfer_id: UUID, body: ReturnRequest, db: Conn, auth: Auth) -> dict[str, Any]:
        if not isinstance(bank, SimulatedBank):
            # Column's sandbox can't return a transfer after the fact. Create the pull with
            # counterparty_name "RETURN_UNAUTH" instead; Column returns it (R29) when it settles.
            raise HTTPException(409, "on Column, create the pull with counterparty_name RETURN_UNAUTH")
        t = await payments.get_transfer(db, auth.space_id, transfer_id)
        if not t["bank_transfer_id"]:
            raise HTTPException(409, "transfer has not reached the bank")
        snapshot = await bank.return_transfer(t["bank_transfer_id"], body.return_code)
        await payments.apply_snapshot(db, bank.provider, auth.space_id, transfer_id, snapshot)
        return await payments.transfer_view(db, auth.space_id, transfer_id)

    @app.post("/webhooks/sim")
    async def webhook(event: dict[str, str], db: Conn) -> dict[str, str]:
        e = BankEvent(event["event_id"], event["bank_transfer_id"], event["type"])
        return {"result": await payments.handle_bank_event(db, bank, e)}

    # S5: write-off with a second approver
    @app.post("/write-offs", status_code=201, dependencies=[Limited])
    async def write_off(db: Conn, auth: Auth) -> dict[str, Any]:
        return await payments.request_write_off(db, auth.space_id, auth.customer_id, f"ops:{auth.customer_id}")

    @app.post("/write-offs/{write_off_id}/approve")
    async def approve(write_off_id: UUID, db: Conn, auth: Auth) -> dict[str, Any]:
        # The demo's second approver is simulated and clearly labelled as such.
        return await payments.approve_write_off(db, auth.space_id, write_off_id, SIMULATED_APPROVER)

    @app.get("/traces")
    async def traces(db: Conn, auth: Auth, limit: int = 20) -> dict[str, Any]:
        """The space's most recent traces, newest first."""
        return {"traces": await tracing.recent(db, auth.space_id, max(1, min(limit, 100)))}

    @app.get("/traces/{trace_id}")
    async def trace(trace_id: str, db: Conn, auth: Auth) -> dict[str, Any]:
        t = await tracing.get(db, auth.space_id, trace_id)
        if t is None:
            raise HTTPException(404, "no such trace in this space")
        return t

    @app.get("/ledger/trial-balance")
    async def trial_balance(db: Conn, auth: Auth) -> dict[str, Any]:
        return await ledger.trial_balance(db, auth.space_id)


app = create_app()
