"""Live status for the Engineering page: is each dependency up, and how fast it has been.

Health is checked now (Postgres, Redis). Latency, call counts and errors come from the stored
traces of the last hour, across all spaces: the same spans the demo shows, aggregated. Nothing
here is per-visitor, so the endpoint needs no session.
"""

import os
import time
from typing import Any

from psycopg import AsyncConnection

from fern import pool
from fern.platform.ratelimit import RateLimiter

# Spans with a parent: every SQL statement, Redis command and outbound call. Roots are requests.
_BY_SERVICE = """
SELECT s->>'service' AS service,
       count(*) AS calls,
       count(*) FILTER (WHERE s->>'status' = 'error') AS errors,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (s->>'duration_us')::bigint) AS p50_us,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY (s->>'duration_us')::bigint) AS p95_us
FROM traces t, jsonb_array_elements(t.spans) s
WHERE t.started_at > now() - interval '1 hour'
  AND s->>'parent_id' IS NOT NULL
  AND s->>'service' IN ('postgres', 'redis', 'column', 'bank-sim', 'plaid', 'openai')
GROUP BY 1
"""

# p95 per 5-minute bucket for the outbound services, for the small charts.
_SERIES = """
SELECT s->>'service' AS service,
       date_bin('5 minutes', t.started_at, now() - interval '1 hour') AS bucket,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY (s->>'duration_us')::bigint) AS p95_us,
       count(*) AS calls
FROM traces t, jsonb_array_elements(t.spans) s
WHERE t.started_at > now() - interval '1 hour'
  AND s->>'parent_id' IS NOT NULL
  AND s->>'service' IN ('column', 'bank-sim', 'plaid', 'openai')
GROUP BY 1, 2 ORDER BY 2
"""

_REQUESTS = """
SELECT count(*) AS requests,
       count(*) FILTER (WHERE status = 'error') AS errors,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_us) AS p50_us,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_us) AS p95_us,
       count(DISTINCT space_id) AS spaces
FROM traces WHERE started_at > now() - interval '1 hour'
"""


async def collect(conn: AsyncConnection, limiter: RateLimiter | None, config: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    await conn.execute("SELECT 1")
    pg_ms = (time.perf_counter() - t0) * 1000

    redis_ms = None
    if limiter is not None:
        t0 = time.perf_counter()
        if await limiter.ping():
            redis_ms = (time.perf_counter() - t0) * 1000

    services = {r["service"]: r for r in await (await conn.execute(_BY_SERVICE)).fetchall()}
    series: dict[str, list[dict[str, Any]]] = {}
    for r in await (await conn.execute(_SERIES)).fetchall():
        series.setdefault(r["service"], []).append(
            {"at": r["bucket"].isoformat(), "p95_us": r["p95_us"], "calls": r["calls"]}
        )
    for name, svc in services.items():
        svc["series"] = series.get(name, [])

    return {
        "checked_at": time.time(),
        "instance": os.environ.get("HOSTNAME", "local"),
        "config": config,
        "checks": {
            "api": {"up": True},
            "postgres": {"up": True, "ping_ms": round(pg_ms, 2)},
            "redis": {"up": redis_ms is not None, "ping_ms": round(redis_ms, 2) if redis_ms is not None else None}
            if limiter is not None
            else {"up": None, "note": "no Redis configured"},
        },
        "requests": await (await conn.execute(_REQUESTS)).fetchone(),
        "services": services,
        "pool": await pool.stats(conn),
    }
