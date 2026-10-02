"""Fixed-window rate limiting in Redis, shared by every API instance."""

from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    max_hits: int
    window_seconds: int


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, limit: Limit, key: str) -> bool:
        """Count one hit; returns False once `key` exceeded the limit in this window."""
        redis_key = f"ratelimit:{limit.name}:{key}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, limit.window_seconds, nx=True)
            count, _ = await pipe.execute()
        return int(count) <= limit.max_hits

    async def exceeded(self, limit: Limit, key: str) -> bool:
        """Check without counting (e.g. failures counted only when they happen)."""
        value = await self._redis.get(f"ratelimit:{limit.name}:{key}")
        return value is not None and int(value) >= limit.max_hits
