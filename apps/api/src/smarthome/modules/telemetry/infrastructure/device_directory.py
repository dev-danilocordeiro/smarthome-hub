"""Answers 'is this device active in this home?' for every telemetry message, cheaply.

Backed by the devices module's public service with a short in-process cache: a
quarantined or revoked device keeps being accepted for at most `ttl_s` seconds here,
but it is already disconnected by the broker, so nothing new arrives from it anyway.
"""

import time
from uuid import UUID

from smarthome.modules.devices.public import DevicesService

SYSTEM_ACTOR = "system:ingestor"


class CachedDeviceDirectory:
    def __init__(self, devices: DevicesService, *, ttl_s: float = 30.0) -> None:
        self._devices = devices
        self._ttl_s = ttl_s
        self._cache: dict[tuple[UUID, str], tuple[bool, float]] = {}

    async def is_active(self, home_id: UUID, device_id: str) -> bool:
        key = (home_id, device_id)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached and now - cached[1] < self._ttl_s:
            return cached[0]
        active = await self._devices.is_active(home_id, device_id)
        self._cache[key] = (active, now)
        return active

    async def quarantine(self, home_id: UUID, device_id: str, *, reason: str) -> None:
        await self._devices.quarantine(home_id, device_id, actor=SYSTEM_ACTOR, reason=reason)
        self._cache[(home_id, device_id)] = (False, time.monotonic())
