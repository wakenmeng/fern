"""Per-request traces (FRONTEND §3).

The demo shows what one request did: every SQL statement, bank call and Redis command, in order
and with timings. Spans are collected in-process (a contextvar holds the current span) and the
finished trace is stored in Postgres under the space it belongs to, so visitors only ever read
their own. The shape follows OpenTelemetry: trace id, span id, parent, service, name, start,
duration, attributes, status.

Instrumented: HTTP requests (api.py middleware), every SQL statement (TracingCursor in db.py),
database transactions (db.transaction), bank calls (column.py, bank.py), Plaid (plaid.py) and
Redis (ratelimit.py). Code outside a trace pays one contextvar lookup per call.
"""

import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


@dataclass
class Span:
    service: str
    name: str
    parent_id: str | None
    attrs: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    start_ns: int = field(default_factory=time.perf_counter_ns)
    end_ns: int | None = None
    status: str = "ok"


@dataclass
class Trace:
    root: Span
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    spans: list[Span] = field(default_factory=list)
    space_id: UUID | None = None

    def to_json(self) -> dict[str, Any]:
        t0, now = self.root.start_ns, time.perf_counter_ns()
        return {
            "trace_id": self.id,
            "name": self.root.name,
            "status": "error" if any(s.status == "error" for s in self.spans) else "ok",
            "duration_us": ((self.root.end_ns or now) - t0) // 1000,
            "spans": [
                {
                    "id": s.id,
                    "parent_id": s.parent_id,
                    "service": s.service,
                    "name": s.name,
                    "start_us": (s.start_ns - t0) // 1000,
                    "duration_us": ((s.end_ns or now) - s.start_ns) // 1000,
                    "status": s.status,
                    "attrs": s.attrs,
                }
                for s in self.spans
            ],
        }


_trace: ContextVar[Trace | None] = ContextVar("fern_trace", default=None)
_span: ContextVar[Span | None] = ContextVar("fern_span", default=None)


def current() -> Trace | None:
    return _trace.get()


def set_space(space_id: UUID) -> None:
    """Attach the current trace to a space; only traces with a space are stored."""
    tr = _trace.get()
    if tr is not None and tr.space_id is None:
        tr.space_id = space_id


def _error(s: Span, exc: BaseException) -> None:
    s.status = "error"
    s.attrs["error.type"] = type(exc).__name__
    if str(exc):
        s.attrs["error.message"] = str(exc)[:300]


@contextmanager
def start(service: str, name: str, **attrs: Any) -> Iterator[Trace]:
    root = Span(service, name, None, dict(attrs))
    tr = Trace(root, spans=[root])
    t1, t2 = _trace.set(tr), _span.set(root)
    try:
        yield tr
    except BaseException as exc:
        _error(root, exc)
        raise
    finally:
        root.end_ns = time.perf_counter_ns()
        _span.reset(t2)
        _trace.reset(t1)


@contextmanager
def span(service: str | None, name: str, **attrs: Any) -> Iterator[Span | None]:
    """A child of the current span. `service=None` keeps the parent's service. No-op outside a trace."""
    tr, parent = _trace.get(), _span.get()
    if tr is None or parent is None:
        yield None
        return
    s = Span(service or parent.service, name, parent.id, dict(attrs))
    tr.spans.append(s)
    token = _span.set(s)
    try:
        yield s
    except BaseException as exc:
        _error(s, exc)
        raise
    finally:
        s.end_ns = time.perf_counter_ns()
        _span.reset(token)


_TABLE = re.compile(r"\b(?:INTO|UPDATE|FROM)\s+(\w+)", re.IGNORECASE)


def sql_name(query: str) -> str:
    """'INSERT journals', 'SELECT accounts FOR UPDATE': the verb and the first table."""
    words = query.split()
    verb = words[0].upper() if words else "SQL"
    m = _TABLE.search(query)
    name = f"{verb} {m.group(1)}" if m else verb
    return name + (" FOR UPDATE" if re.search(r"\bFOR UPDATE\b", query, re.IGNORECASE) else "")


# --------------------------------------------------------------------------- storage


async def save(conn: AsyncConnection, tr: Trace) -> None:
    if tr.space_id is None:
        return
    data = tr.to_json()
    token = _trace.set(None)  # writing the trace is not part of it
    try:
        await conn.execute(
            "INSERT INTO traces (id, space_id, name, status, duration_us, spans) VALUES (%s, %s, %s, %s, %s, %s)",
            (tr.id, tr.space_id, data["name"], data["status"], data["duration_us"], Jsonb(data["spans"])),
        )
    finally:
        _trace.reset(token)


async def recent(conn: AsyncConnection, space_id: UUID, limit: int = 20) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT id AS trace_id, name, status, duration_us, started_at, jsonb_array_length(spans) AS span_count "
        "FROM traces WHERE space_id = %s ORDER BY started_at DESC LIMIT %s",
        (space_id, limit),
    )
    return await cur.fetchall()


async def get(conn: AsyncConnection, space_id: UUID, trace_id: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT id AS trace_id, name, status, duration_us, started_at, spans FROM traces "
        "WHERE space_id = %s AND id = %s",
        (space_id, trace_id),
    )
    return await cur.fetchone()
