"""A home with two smart plugs and a whole-home meter, over real Postgres and Redis."""

import secrets
from dataclasses import dataclass

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from device_protocol import DeviceKind
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.devices.infrastructure.live_state import RedisLiveState
from smarthome.modules.devices.infrastructure.persistence import PostgresDevicesUnitOfWork
from smarthome.modules.identity.application.services import IdentityService
from smarthome.modules.identity.domain.model import HomeId
from smarthome.modules.identity.infrastructure.persistence import PostgresUnitOfWork
from smarthome.shared.clock import SystemClock
from tests.integration.automations.conftest import AcceptingBroker, api
from tests.integration.helpers import Member, sign_in

__all__ = ["api"]


@pytest.fixture
def device_service(engine: AsyncEngine, redis: Redis) -> DevicesService:
    return DevicesService(
        lambda: PostgresDevicesUnitOfWork(engine),
        AcceptingBroker(),
        RedisLiveState(redis),
        SystemClock(),
    )


@dataclass(frozen=True)
class Household:
    home: HomeId
    owner: Member
    identity: IdentityService
    devices: dict[str, str]  # name -> device id


@pytest.fixture
async def household(engine: AsyncEngine, redis: Redis, device_service: DevicesService) -> Household:
    identity = IdentityService(lambda: PostgresUnitOfWork(engine), SystemClock())
    owner = await sign_in(redis, f"owner-{secrets.token_hex(4)}")
    home = (
        await identity.create_home(owner.principal, name="Casa", timezone="America/Sao_Paulo")
    ).id
    paired: dict[str, str] = {}
    for name, kind in {
        "meter": DeviceKind.ENERGY_METER,
        "fridge": DeviceKind.PLUG,
        "washer": DeviceKind.PLUG,
        "hall": DeviceKind.LIGHT,
    }.items():
        _, code = await device_service.create_pairing_code(
            home_id=home, actor=owner.principal.user_id, name=name.title()
        )
        claimed = await device_service.claim(code=code, kind=kind, firmware=None)
        paired[name] = claimed.device.id
    return Household(home, owner, identity, paired)


@pytest.fixture
async def fresh_rollup(engine: AsyncEngine) -> None:
    """The rollup cursor is global; each test starts as if the rollup never ran."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM energy.rollup_cursor"))
