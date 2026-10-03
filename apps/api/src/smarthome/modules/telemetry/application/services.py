from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog

from smarthome.modules.telemetry.application.buffer import TelemetryBuffer
from smarthome.modules.telemetry.application.ports import (
    Deduplicator,
    DeviceDirectory,
    FloodGuard,
    LatestReadings,
    ReadingsQueries,
)
from smarthome.modules.telemetry.domain.errors import InvalidRange
from smarthome.modules.telemetry.domain.model import (
    MAX_RANGE,
    HourlyIncrease,
    Point,
    Resolution,
    choose_resolution,
    readings_from_message,
)
from smarthome.shared.events import (
    DeviceEvent,
    DeviceEventKind,
    EventPublisher,
    NullEventPublisher,
)

log = structlog.get_logger(__name__)


class TelemetryIngest:
    """Validated telemetry message → guarded, deduplicated rows in the write buffer."""

    def __init__(
        self,
        *,
        buffer: TelemetryBuffer,
        latest: LatestReadings,
        dedup: Deduplicator,
        flood: FloodGuard,
        devices: DeviceDirectory,
        events: EventPublisher | None = None,
    ) -> None:
        self._events = events or NullEventPublisher()
        self._buffer = buffer
        self._latest = latest
        self._dedup = dedup
        self._flood = flood
        self._devices = devices

    async def accept(
        self, *, home_id: UUID, device_id: str, payload: dict[str, Any], received_at: datetime
    ) -> bool:
        if not await self._devices.is_active(home_id, device_id):
            return False
        if await self._flood.over_limit(device_id):
            if await self._flood.first_trip(device_id):
                log.warning("device_flooding_quarantined", device_id=device_id)
                await self._devices.quarantine(
                    home_id, device_id, reason="telemetry rate limit exceeded"
                )
            return False
        if not await self._dedup.first_time(device_id, payload["message_id"]):
            return False
        readings = readings_from_message(
            home_id=home_id, device_id=device_id, payload=payload, received_at=received_at
        )
        await self._latest.update(readings)
        await self._buffer.put(readings)
        if readings:
            await self._events.publish(
                DeviceEvent(
                    home_id,
                    device_id,
                    DeviceEventKind.TELEMETRY,
                    readings[0].time,
                    dict(payload["readings"]),
                )
            )
        return True


class TelemetryQueries:
    def __init__(self, queries: ReadingsQueries, latest: LatestReadings) -> None:
        self._queries = queries
        self._latest = latest

    async def series(
        self,
        *,
        home_id: UUID,
        device_id: str,
        metric: str,
        start: datetime,
        end: datetime | None = None,
        resolution: Resolution | None = None,
    ) -> tuple[Resolution, list[Point]]:
        end = end or datetime.now(UTC)
        if start >= end:
            raise InvalidRange("`from` must be before `to`")
        if end - start > MAX_RANGE:
            raise InvalidRange("range too long")
        chosen = resolution or choose_resolution(start, end)
        points = await self._queries.series(
            home_id=home_id,
            device_id=device_id,
            metric=metric,
            start=start,
            end=end,
            resolution=chosen,
        )
        return chosen, points

    async def hourly_increase(
        self, *, metric: str, start: datetime, end: datetime
    ) -> list[HourlyIncrease]:
        """Per device and hour, how much a cumulative counter grew in [start, end), across
        every home. Counter resets (device reboots) are not negative consumption."""
        if start >= end:
            raise InvalidRange("`start` must be before `end`")
        return await self._queries.hourly_increase(metric=metric, start=start, end=end)

    async def latest(self, device_id: str) -> dict[str, dict[str, object]]:
        return await self._latest.get(device_id)
