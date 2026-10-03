from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


class DeviceEventKind(StrEnum):
    STATE = "state"  # data: the full reported state
    PRESENCE = "presence"  # data: {"online": bool}
    TELEMETRY = "telemetry"  # data: the readings of one message
    COMMAND = "command"  # data: {"command_id", "status", "reason"} once an outcome is known


@dataclass(frozen=True, slots=True)
class DeviceEvent:
    """Something a device reported that changed the hub's view of it.

    `at` is the time the change is ordered by: the device's timestamp for state and
    telemetry, the hub's for presence (the Last Will carries none).
    """

    home_id: UUID
    device_id: str
    kind: DeviceEventKind
    at: datetime
    data: dict[str, Any]


class EventPublisher(Protocol):
    async def publish(self, event: DeviceEvent) -> None:
        """Best effort: called after the change is committed, never raises."""
        ...


class NullEventPublisher:
    """For entrypoints that never produce device events (the API)."""

    async def publish(self, event: DeviceEvent) -> None:
        return None
