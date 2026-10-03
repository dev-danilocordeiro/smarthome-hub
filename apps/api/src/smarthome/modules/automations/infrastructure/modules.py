"""Ports backed by other modules' public interfaces (devices, telemetry, commands)."""

from collections.abc import Collection
from datetime import datetime
from typing import Any
from uuid import UUID

from device_protocol import DeviceKind
from smarthome.modules.automations.domain.definition import Source, SourceKind
from smarthome.modules.automations.domain.engine import Observation
from smarthome.modules.automations.domain.model import ActionOutcome
from smarthome.modules.commands.public import CommandAction, CommandsError, CommandsService
from smarthome.modules.devices.public import DeviceNotFound, DevicesService
from smarthome.modules.telemetry.public import Resolution, TelemetryQueries


class DevicesAdapter:
    def __init__(self, devices: DevicesService, telemetry: TelemetryQueries) -> None:
        self._devices = devices
        self._telemetry = telemetry

    async def kinds(self, home_id: UUID) -> dict[str, DeviceKind]:
        return {d.id: d.kind for d in await self._devices.list_devices(home_id)}

    async def current(
        self, home_id: UUID, sources: Collection[Source]
    ) -> dict[Source, Observation | None]:
        found: dict[Source, Observation | None] = {}
        for source in sources:
            found[source] = await self._read(home_id, source)
        return found

    async def _read(self, home_id: UUID, source: Source) -> Observation | None:
        if source.kind is SourceKind.TELEMETRY:
            return await self._latest_reading(source)
        try:
            view = await self._devices.get(home_id, source.device_id)
        except DeviceNotFound:
            return None
        if source.kind is SourceKind.PRESENCE:
            device = view.device
            if device.presence_changed_at is None:
                return None
            return Observation(device.online, device.presence_changed_at)
        twin = view.twin
        if source.key not in twin.reported or twin.reported_at is None:
            return None
        return Observation(twin.reported[source.key], twin.reported_at)

    async def _latest_reading(self, source: Source) -> Observation | None:
        latest = (await self._telemetry.latest(source.device_id)).get(source.key)
        if latest is None:
            return None
        return Observation(latest["value"], datetime.fromisoformat(str(latest["at"])))


class TelemetryHistoryAdapter:
    def __init__(self, telemetry: TelemetryQueries) -> None:
        self._telemetry = telemetry

    async def series(
        self, home_id: UUID, device_id: str, metric: str, *, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        _, points = await self._telemetry.series(
            home_id=home_id,
            device_id=device_id,
            metric=metric,
            start=start,
            end=end,
            resolution=Resolution.RAW,
        )
        return [(p.time, p.avg) for p in points]


class CommandsAdapter:
    def __init__(self, commands: CommandsService) -> None:
        self._commands = commands

    async def issue(
        self,
        *,
        home_id: UUID,
        device_id: str,
        action: str,
        desired: dict[str, Any] | None,
        actor: str,
    ) -> ActionOutcome:
        try:
            command = await self._commands.issue(
                home_id=home_id,
                device_id=device_id,
                action=CommandAction(action),
                desired=desired,
                actor=actor,
            )
        except CommandsError as exc:
            return ActionOutcome(device_id, error=f"{type(exc).__name__}: {exc}"[:200])
        return ActionOutcome(device_id, command_id=command.id)
