"""A home with paired devices and the automations stack over real Postgres and Redis.

Pairing goes through the devices service with a broker that accepts everything: these
tests are about what happens after a device reports, which needs no MQTT.
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from device_protocol import DeviceKind
from smarthome.modules.automations import wiring as automations
from smarthome.modules.automations.application.engine import AutomationEngine
from smarthome.modules.automations.application.services import AutomationsService
from smarthome.modules.commands import wiring as commands
from smarthome.modules.commands.application.services import CommandsService
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.devices.infrastructure.live_state import RedisLiveState
from smarthome.modules.devices.infrastructure.persistence import PostgresDevicesUnitOfWork
from smarthome.modules.identity.application.services import IdentityService
from smarthome.modules.identity.domain.model import HomeId
from smarthome.modules.identity.infrastructure.persistence import PostgresUnitOfWork
from smarthome.modules.telemetry import wiring as telemetry
from smarthome.modules.telemetry.application.services import TelemetryQueries
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings
from smarthome.shared.infrastructure.event_stream import RedisEventPublisher
from tests.integration.conftest import client_for
from tests.integration.helpers import Member, sign_in


class AcceptingBroker:
    async def register_device(self, *, home_id: str, device_id: str, password: str) -> None:
        return None

    async def disable_device(self, device_id: str) -> None:
        return None

    async def enable_device(self, device_id: str) -> None:
        return None

    async def remove_device(self, device_id: str) -> None:
        return None


@pytest.fixture
def stream() -> str:
    """Each test gets its own stream, so consumer groups never see another test's events."""
    return f"test:events:{secrets.token_hex(4)}"


@pytest.fixture
def publisher(redis: Redis, stream: str) -> RedisEventPublisher:
    return RedisEventPublisher(redis, stream=stream, maxlen=10_000)


@pytest.fixture
def device_service(
    engine: AsyncEngine, redis: Redis, publisher: RedisEventPublisher
) -> DevicesService:
    return DevicesService(
        lambda: PostgresDevicesUnitOfWork(engine),
        AcceptingBroker(),
        RedisLiveState(redis),
        SystemClock(),
        events=publisher,
    )


@pytest.fixture
def queries(engine: AsyncEngine, redis: Redis) -> TelemetryQueries:
    return telemetry.build_queries(engine=engine, redis=redis)


@pytest.fixture
def command_service(
    *, migrated_database: Settings, engine: AsyncEngine, device_service: DevicesService
) -> CommandsService:
    return commands.build_service(
        migrated_database, engine=engine, devices=device_service, clock=SystemClock()
    )


@pytest.fixture
def automation_service(
    *,
    migrated_database: Settings,
    engine: AsyncEngine,
    redis: Redis,
    device_service: DevicesService,
    queries: TelemetryQueries,
    command_service: CommandsService,
) -> AutomationsService:
    return automations.build_service(
        migrated_database,
        engine=engine,
        redis=redis,
        devices=device_service,
        telemetry=queries,
        commands=command_service,
        clock=SystemClock(),
    )


def build_engine(
    *,
    settings: Settings,
    engine: AsyncEngine,
    redis: Redis,
    devices: DevicesService,
    queries: TelemetryQueries,
    commands_: CommandsService,
) -> AutomationEngine:
    return automations.build_engine(
        settings,
        engine=engine,
        redis=redis,
        devices=devices,
        telemetry=queries,
        commands=commands_,
        clock=SystemClock(),
    )


@pytest.fixture
def automation_engine(
    *,
    migrated_database: Settings,
    engine: AsyncEngine,
    redis: Redis,
    device_service: DevicesService,
    queries: TelemetryQueries,
    command_service: CommandsService,
) -> AutomationEngine:
    return build_engine(
        settings=migrated_database,
        engine=engine,
        redis=redis,
        devices=device_service,
        queries=queries,
        commands_=command_service,
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
        "hall": DeviceKind.LIGHT,
        "porch": DeviceKind.LIGHT,
        "motion": DeviceKind.MOTION_SENSOR,
        "climate": DeviceKind.CLIMATE_SENSOR,
        "door": DeviceKind.LOCK,
    }.items():
        _, code = await device_service.create_pairing_code(
            home_id=home, actor=owner.principal.user_id
        )
        claimed = await device_service.claim(code=code, kind=kind, firmware=None)
        paired[name] = claimed.device.id
    return Household(home, owner, identity, paired)


@pytest.fixture
async def api(migrated_database: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async for client in client_for(migrated_database):
        yield client
