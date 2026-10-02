"""Redis read model of each device's live state: presence and last reported twin.

Dashboards read this, never the database (and later never TimescaleDB). Writes are
last-writer-wins guarded by the event timestamp, so a delayed message cannot roll the
view back.
"""

import json
from datetime import datetime
from typing import Any

from redis.asyncio import Redis

KEY = "device:{}"

# Set the field only if the stored timestamp for it is older (or missing).
_SET_IF_NEWER = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current and tonumber(current) >= tonumber(ARGV[2]) then return 0 end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2], ARGV[3], ARGV[4])
return 1
"""


class RedisLiveState:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def set_presence(self, device_id: str, *, online: bool, at: datetime) -> None:
        await self._set_if_newer(device_id, "presence_ts", at, "online", "1" if online else "0")

    async def set_reported(self, device_id: str, reported: dict[str, Any], *, at: datetime) -> None:
        await self._set_if_newer(device_id, "reported_ts", at, "reported", json.dumps(reported))

    async def forget(self, device_id: str) -> None:
        await self._redis.delete(KEY.format(device_id))

    async def get(self, device_id: str) -> dict[str, Any]:
        stored = await self._redis.hgetall(KEY.format(device_id))
        raw = {str(k): str(v) for k, v in stored.items()}
        return {
            "online": raw.get("online") == "1",
            "reported": json.loads(raw["reported"]) if "reported" in raw else None,
        }

    async def _set_if_newer(
        self, device_id: str, ts_field: str, at: datetime, field: str, value: str
    ) -> None:
        await self._redis.eval(
            _SET_IF_NEWER, 1, KEY.format(device_id), ts_field, str(at.timestamp()), field, value
        )
