"""Redis adapters: causality marks for the loop guard, and the engine's automation cache."""

import time
from collections.abc import Collection
from uuid import UUID

from redis.asyncio import Redis

from smarthome.modules.automations.application.ports import AutomationsUnitOfWorkFactory
from smarthome.modules.automations.domain.model import Automation

CAUSE_KEY = "automations:cause:{}"
GENERATION_KEY = "automations:generation:{}"


class RedisCausality:
    """`automations:cause:<device>` = depth of the run whose command targets the device.

    Expires after the command could still be in flight (TTL + ack grace + slack), so a
    later change by a person starts a new chain at depth 0.
    """

    def __init__(self, redis: Redis, *, ttl_s: int) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    async def depth(self, device_id: str) -> int:
        value = await self._redis.get(CAUSE_KEY.format(device_id))
        return int(value) if value is not None else 0

    async def caused(self, device_ids: Collection[str], *, depth: int) -> None:
        if not device_ids:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            for device_id in device_ids:
                pipe.set(CAUSE_KEY.format(device_id), depth, ex=self._ttl_s)
            await pipe.execute()


class CachedDirectory:
    """Armed automations per home, cached in the worker.

    Each home has a generation counter in Redis, bumped by every change (from the API
    or the engine). A cached entry is used while its generation is current, so an edit
    reaches every worker on its next event, at the cost of one GET per event. The TTL
    bounds staleness if a bump is ever lost.
    """

    def __init__(
        self, redis: Redis, uow: AutomationsUnitOfWorkFactory, *, max_age_s: float = 60.0
    ) -> None:
        self._redis = redis
        self._uow = uow
        self._max_age_s = max_age_s
        self._cache: dict[UUID, tuple[str, float, list[Automation]]] = {}

    async def armed(self, home_id: UUID) -> list[Automation]:
        generation = str(await self._redis.get(GENERATION_KEY.format(home_id)) or "0")
        cached = self._cache.get(home_id)
        if (
            cached is not None
            and cached[0] == generation
            and time.monotonic() - cached[1] < self._max_age_s
        ):
            return cached[2]
        async with self._uow() as uow:
            armed = [a for a in await uow.automations.for_home(home_id) if a.armed]
        self._cache[home_id] = (generation, time.monotonic(), armed)
        return armed

    async def invalidate(self, home_id: UUID) -> None:
        self._cache.pop(home_id, None)
        await self._redis.incr(GENERATION_KEY.format(home_id))
