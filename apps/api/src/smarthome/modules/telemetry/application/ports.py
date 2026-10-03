from datetime import datetime
from typing import Protocol
from uuid import UUID

from smarthome.modules.telemetry.domain.model import HourlyIncrease, Point, Reading, Resolution


class ReadingsWriter(Protocol):
    async def write(self, readings: list[Reading]) -> int:
        """Insert, skipping duplicates; returns how many rows were new."""
        ...


class ReadingsQueries(Protocol):
    async def series(
        self,
        *,
        home_id: UUID,
        device_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        resolution: Resolution,
    ) -> list[Point]: ...

    async def hourly_increase(
        self, *, metric: str, start: datetime, end: datetime
    ) -> list[HourlyIncrease]: ...


class LatestReadings(Protocol):
    async def update(self, readings: list[Reading]) -> None: ...
    async def get(self, device_id: str) -> dict[str, dict[str, object]]: ...


class Deduplicator(Protocol):
    async def first_time(self, device_id: str, message_id: str) -> bool:
        """True the first time a message id is seen (within a retention window)."""
        ...


class FloodGuard(Protocol):
    async def over_limit(self, device_id: str) -> bool:
        """Count one message; True if the device exceeded its per-minute budget."""
        ...

    async def first_trip(self, device_id: str) -> bool:
        """True only once per incident, so a flooding device is quarantined once."""
        ...


class DeviceDirectory(Protocol):
    async def is_active(self, home_id: UUID, device_id: str) -> bool: ...
    async def quarantine(self, home_id: UUID, device_id: str, *, reason: str) -> None: ...
