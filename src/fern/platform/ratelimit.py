"""Per-space rate limit on money-moving requests: a token bucket in Redis (DESIGN §3, §11).

Redis holds no money state. If it is unreachable the limiter lets the request through and says
so in the trace: losing the limiter must not take payments down with it.
"""

import time

import redis.asyncio as redis

from fern.platform import tracing

# Refill, take one token if there is one, store. Atomic because it runs as one script.
_TOKEN_BUCKET = """
local cap, rate, now = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = math.min(cap, (tonumber(b[1]) or cap) + (now - (tonumber(b[2]) or now)) * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(cap / rate) + 1)
return {allowed, tostring(tokens)}
"""


class RateLimiter:
    def __init__(self, url: str, per_minute: int) -> None:
        self.capacity, self.rate = per_minute, per_minute / 60
        self._redis = redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self._script = self._redis.register_script(_TOKEN_BUCKET)

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def allow(self, scope: str) -> bool:
        key = f"rl:{{{scope}}}"
        attrs = {"db.system": "redis", "db.statement": f"EVALSHA token_bucket 1 {key} {self.capacity} {self.rate:.3f}"}
        with tracing.span("redis", "EVALSHA token_bucket", **attrs) as s:
            try:
                allowed, left = await self._script(keys=[key], args=[self.capacity, self.rate, time.time()])
            except redis.RedisError as e:
                if s is not None:
                    s.attrs["result"] = f"Redis unavailable ({type(e).__name__}): allowed without limiting"
                return True
            if s is not None:
                s.attrs["result"] = f"{'allowed' if allowed else 'limited'} · {float(left):.0f} of {self.capacity} left"
            return bool(allowed)
