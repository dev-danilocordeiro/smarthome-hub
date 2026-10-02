"""Real Postgres (TimescaleDB) and Redis via Testcontainers. No fakes for storage."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import PostgresDsn, RedisDsn
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from smarthome.main import create_app
from smarthome.shared.config import Environment, Settings
from smarthome.shared.infrastructure.db import create_engine
from smarthome.shared.infrastructure.redis import create_redis

# Keep in sync with infra/docker-compose.yml.
TIMESCALE_IMAGE = "timescale/timescaledb:2.30.2-pg16"
REDIS_IMAGE = "redis:7.4.11-alpine"


@pytest.fixture(scope="session")
def span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Global providers can be set once per process; every test in the session shares them.
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture(scope="session")
def metric_reader() -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return reader


@pytest.fixture(scope="session", autouse=True)
def in_memory_telemetry(
    span_exporter: InMemorySpanExporter, metric_reader: InMemoryMetricReader
) -> None:
    """Install in-memory providers before any app is built, as an entrypoint would."""


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
        diagnostics_enabled=True,
    )


@pytest.fixture(scope="session")
def migrated_database(settings: Settings, api_root: Path) -> Settings:
    config = Config(str(api_root / "alembic.ini"))
    config.attributes["database_url"] = str(settings.database_url)
    command.upgrade(config, "head")
    return settings


@pytest.fixture
async def engine(migrated_database: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated_database)
    yield engine
    await engine.dispose()


@pytest.fixture
async def redis(migrated_database: Settings) -> AsyncIterator[Redis]:
    client = create_redis(migrated_database)
    yield client
    await client.aclose()


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
