"""Redis adapters: latest readings (dashboard reads), message dedup, per-device flood guard."""

import json
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis

from smarthome.modules.telemetry.domain.model import Reading

if TYPE_CHECKING:
    from redis.typing import EncodableT, FieldT

LATEST_KEY = "device:{}:readings"
DEDUP_KEY = "telemetry:seen:{}:{}"
DEDUP_TTL_S = 600
FLOOD_KEY = "telemetry:rate:{}:{}"
TRIP_KEY = "telemetry:flood-tripped:{}"
TRIP_TTL_S = 3600


class RedisLatestReadings:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def update(self, readings: list[Reading]) -> None:
        by_device: dict[str, dict[FieldT, EncodableT]] = defaultdict(dict)
        for r in readings:
            by_device[r.device_id][r.metric] = json.dumps(
                {"value": r.value, "at": r.time.isoformat()}
            )
        async with self._redis.pipeline(transaction=False) as pipe:
            for device_id, fields in by_device.items():
                pipe.hset(LATEST_KEY.format(device_id), mapping=fields)
            await pipe.execute()

    async def get(self, device_id: str) -> dict[str, dict[str, Any]]:
        stored = await self._redis.hgetall(LATEST_KEY.format(device_id))
        return {str(k): json.loads(str(v)) for k, v in stored.items()}


class RedisDeduplicator:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def first_time(self, device_id: str, message_id: str) -> bool:
        return bool(
            await self._redis.set(
                DEDUP_KEY.format(device_id, message_id), "1", nx=True, ex=DEDUP_TTL_S
            )
        )


class RedisFloodGuard:
    def __init__(self, redis: Redis, *, max_per_minute: int) -> None:
        self._redis = redis
        self._max = max_per_minute

    async def over_limit(self, device_id: str) -> bool:
        key = FLOOD_KEY.format(device_id, int(time.time() // 60))
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 120)
            count, _ = await pipe.execute()
        return int(count) > self._max

    async def first_trip(self, device_id: str) -> bool:
        return bool(await self._redis.set(TRIP_KEY.format(device_id), "1", nx=True, ex=TRIP_TTL_S))
