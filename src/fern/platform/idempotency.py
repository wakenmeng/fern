"""API idempotency (DESIGN §5.7). The key row is written in the same transaction as the work,
so a committed key always has a committed result. A concurrent duplicate blocks on the
uncommitted row, then replays the stored response."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


class IdempotencyKeyReused(Exception):
    """Same key, different request body."""


@dataclass(frozen=True)
class Response:
    status_code: int
    body: dict[str, Any]


def request_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


async def claim(
    conn: AsyncConnection, *, space_id: UUID, principal: str, operation: str, key: str, req_hash: str
) -> Response | None:
    """Claim the key for this request, or return the stored response of an earlier one."""
    cur = await conn.execute(
        "INSERT INTO idempotency_requests (space_id, principal, operation, idem_key, request_hash, status) "
        "VALUES (%s, %s, %s, %s, %s, 'in_progress') ON CONFLICT DO NOTHING RETURNING 1",
        (space_id, principal, operation, key, req_hash),
    )
    if await cur.fetchone():
        return None
    cur = await conn.execute(
        "SELECT request_hash, response_status, response_body FROM idempotency_requests "
        "WHERE space_id = %s AND principal = %s AND operation = %s AND idem_key = %s",
        (space_id, principal, operation, key),
    )
    row = await cur.fetchone()
    assert row is not None
    if row["request_hash"] != req_hash:
        raise IdempotencyKeyReused(key)
    return Response(row["response_status"], row["response_body"])


async def save(
    conn: AsyncConnection, *, space_id: UUID, principal: str, operation: str, key: str, response: Response
) -> None:
    await conn.execute(
        "UPDATE idempotency_requests SET status = 'completed', response_status = %s, response_body = %s "
        "WHERE space_id = %s AND principal = %s AND operation = %s AND idem_key = %s",
        (response.status_code, Jsonb(response.body), space_id, principal, operation, key),
    )
