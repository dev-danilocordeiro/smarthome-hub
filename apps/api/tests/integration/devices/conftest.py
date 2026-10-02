from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.devices import wiring as devices
from smarthome.modules.devices.application.services import DevicesService
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings
from tests.integration.conftest import client_for
from tests.integration.mqtt_broker import ADMIN_PASSWORD, ADMIN_USER, Broker, broker

__all__ = ["broker"]

HUB_PASSWORD = "integration-hub"


@pytest.fixture
def device_settings(migrated_database: Settings, broker: Broker) -> Settings:
    return Settings.model_validate(
        migrated_database.model_dump()
        | {
            "mqtt_host": broker.host,
            "mqtt_port": broker.port,
            "mqtt_ca_file": broker.ca_file,
            "mqtt_admin_user": ADMIN_USER,
            "mqtt_admin_password": SecretStr(ADMIN_PASSWORD),
            "mqtt_hub_password": SecretStr(HUB_PASSWORD),
            "device_broker_host": broker.host,
            "device_broker_port": broker.port,
            "claim_rate_per_minute": 1000,
        }
    )


@pytest.fixture
def service(device_settings: Settings, engine: AsyncEngine, redis: Redis) -> DevicesService:
    return devices.build_service(device_settings, engine=engine, redis=redis, clock=SystemClock())


@pytest.fixture
async def api(device_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async for client in client_for(device_settings):
        yield client
