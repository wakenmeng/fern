from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from fern.platform import tracing

# Deadlocks and serialization failures: retry the whole database transaction.
_RETRYABLE = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)


class TracingCursor(AsyncCursor):
    """Records every statement as a span when a trace is active."""

    async def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        if tracing.current() is None:
            return await super().execute(query, params, **kwargs)
        text = query if isinstance(query, str) else query.as_string(self.connection)
        attrs = {"db.system": "postgresql", "db.statement": " ".join(text.split())}
        if params:
            attrs["db.params"] = repr(params)[:300]
        with tracing.span("postgres", tracing.sql_name(text), **attrs) as s:
            result = await super().execute(query, params, **kwargs)
            if s is not None:
                s.attrs["db.rows"] = self.rowcount
            return result


def create_pool(conninfo: str, *, max_size: int = 20) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo,
        min_size=1,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "autocommit": True, "cursor_factory": TracingCursor},
        open=False,
    )


async def transaction[T](
    conn: AsyncConnection, work: Callable[[AsyncConnection], Awaitable[T]], *, name: str | None = None
) -> T:
    """Run `work` in one database transaction, retrying up to 3 times on deadlock."""
    name = name or work.__qualname__.split(".<locals>")[0]
    with tracing.span(None, f"db.tx {name}", **{"code.function": f"{work.__module__}.{name}"}) as s:
        for attempt in range(3):
            try:
                async with conn.transaction():
                    return await work(conn)
            except _RETRYABLE:
                if s is not None:
                    s.attrs["db.retries"] = attempt + 1
                if attempt == 2:
                    raise
    raise AssertionError("unreachable")
