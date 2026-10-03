"""Ports backed by other modules' public interfaces (identity, devices, energy)."""

from uuid import UUID

from smarthome.modules.devices.public import DeviceNotFound, DevicesService
from smarthome.modules.energy.public import EnergyService
from smarthome.modules.identity.public import HomeId, IdentityService
from smarthome.modules.notifications.application.ports import Audience, Budget, Recipient
from smarthome.modules.notifications.domain.rules import DeviceInfo


class IdentityHomes:
    def __init__(self, identity: IdentityService) -> None:
        self._identity = identity

    async def audience(self, home_id: UUID) -> Audience | None:
        found = await self._identity.audience(HomeId(home_id))
        if found is None:
            return None
        return Audience(
            found.timezone, tuple(Recipient(r.user_id, r.email) for r in found.recipients)
        )


class DevicesDirectory:
    def __init__(self, devices: DevicesService) -> None:
        self._devices = devices

    async def info(self, home_id: UUID, device_id: str) -> DeviceInfo | None:
        try:
            device = (await self._devices.get(home_id, device_id)).device
        except DeviceNotFound:
            return None
        return DeviceInfo(device.id, device.name, device.kind)

    async def is_online(self, home_id: UUID, device_id: str) -> bool | None:
        try:
            return (await self._devices.get(home_id, device_id)).device.online
        except DeviceNotFound:
            return None


class EnergyBudgets:
    def __init__(self, energy: EnergyService) -> None:
        self._energy = energy

    async def budgets(self) -> list[Budget]:
        return [
            Budget(b.home_id, b.month_start, float(b.used_kwh), float(b.budget_kwh))
            for b in await self._energy.budgets()
        ]
