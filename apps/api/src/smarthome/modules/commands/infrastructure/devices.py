"""The `Devices` port, backed by the devices module's public service."""

from datetime import datetime
from typing import Any
from uuid import UUID

from smarthome.modules.commands.application.ports import Target
from smarthome.modules.devices.public import DeviceNotFound, DevicesService, DeviceStatus


class DevicesModuleAdapter:
    def __init__(self, devices: DevicesService) -> None:
        self._devices = devices

    async def target(self, home_id: UUID, device_id: str) -> Target | None:
        try:
            device = (await self._devices.get(home_id, device_id)).device
        except DeviceNotFound:
            return None
        return Target(
            device_id=device.id,
            home_id=device.home_id,
            kind=device.kind,
            accepts_commands=device.status is DeviceStatus.ACTIVE,
        )

    async def set_desired(
        self, home_id: UUID, device_id: str, desired: dict[str, Any], *, at: datetime
    ) -> None:
        await self._devices.set_desired(
            home_id=home_id, device_id=device_id, desired=desired, at=at
        )
