"""Real Postgres (TimescaleDB) and Redis via Testcontainers. No fakes for storage."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from pydantic import PostgresDsn, RedisDsn
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from smarthome.main import create_app
from smarthome.shared.config import Environment, Settings

# Keep in sync with infra/docker-compose.yml.
TIMESCALE_IMAGE = "timescale/timescaledb:2.30.2-pg16"
REDIS_IMAGE = "redis:7.4.11-alpine"


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer(TIMESCALE_IMAGE, driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer(REDIS_IMAGE) as container:
        yield container


@pytest.fixture(scope="session")
def settings(postgres: PostgresContainer, redis_container: RedisContainer) -> Settings:
    redis_host = redis_container.get_container_host_ip()
    redis_port = redis_container.get_exposed_port(6379)
    return Settings(
        environment=Environment.TEST,
        log_json=False,
        database_url=PostgresDsn(postgres.get_connection_url()),
        redis_url=RedisDsn(f"redis://{redis_host}:{redis_port}/0"),
        readiness_timeout_seconds=1.0,
    )


@pytest.fixture(scope="session")
def migrated_database(settings: Settings, api_root: Path) -> Settings:
    config = Config(str(api_root / "alembic.ini"))
    config.attributes["database_url"] = str(settings.database_url)
    command.upgrade(config, "head")
    return settings


async def client_for(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield client


@pytest.fixture
async def client(migrated_database: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async for c in client_for(migrated_database):
        yield c
