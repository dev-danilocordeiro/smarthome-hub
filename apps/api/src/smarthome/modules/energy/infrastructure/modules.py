"""Ports backed by other modules' public interfaces (telemetry, devices)."""

from datetime import datetime
from uuid import UUID

from device_protocol import DeviceKind
from smarthome.modules.devices.public import DevicesService
from smarthome.modules.energy.application.ports import Increase, MeteredDevice
from smarthome.modules.telemetry.public import TelemetryQueries

COUNTER_METRIC = "energy_wh_total"
METERED_KINDS = frozenset({DeviceKind.PLUG, DeviceKind.ENERGY_METER})


class TelemetryCounters:
    def __init__(self, telemetry: TelemetryQueries) -> None:
        self._telemetry = telemetry

    async def hourly_increase(self, *, start: datetime, end: datetime) -> list[Increase]:
        return [
            Increase(r.home_id, r.device_id, r.hour, r.increase)
            for r in await self._telemetry.hourly_increase(
                metric=COUNTER_METRIC, start=start, end=end
            )
        ]


class MeteredDevices:
    def __init__(self, devices: DevicesService) -> None:
        self._devices = devices

    async def metered(self, home_id: UUID) -> list[MeteredDevice]:
        return [
            MeteredDevice(d.id, d.name, d.room, whole_home=d.kind is DeviceKind.ENERGY_METER)
            for d in await self._devices.list_devices(home_id)
            if d.kind in METERED_KINDS
        ]
