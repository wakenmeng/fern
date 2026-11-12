from collections.abc import Awaitable, Callable

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# Deadlocks and serialization failures: retry the whole database transaction.
_RETRYABLE = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)


def create_pool(conninfo: str, *, max_size: int = 20) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo,
        min_size=1,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )


async def transaction[T](conn: AsyncConnection, work: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    """Run `work` in one database transaction, retrying up to 3 times on deadlock."""
    for attempt in range(3):
        try:
            async with conn.transaction():
                return await work(conn)
        except _RETRYABLE:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")
