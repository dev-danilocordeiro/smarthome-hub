"""Composition of the telemetry module for an entrypoint. Only entrypoints import this."""

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.devices.public import DevicesService
from smarthome.modules.telemetry.api import errors, routes
from smarthome.modules.telemetry.api.routes import TelemetryModule
from smarthome.modules.telemetry.application.buffer import TelemetryBuffer
from smarthome.modules.telemetry.application.services import TelemetryIngest, TelemetryQueries
from smarthome.modules.telemetry.infrastructure.device_directory import CachedDeviceDirectory
from smarthome.modules.telemetry.infrastructure.redis_stores import (
    RedisDeduplicator,
    RedisFloodGuard,
    RedisLatestReadings,
)
from smarthome.modules.telemetry.infrastructure.timescale import TimescaleReadings, apply_retention
from smarthome.shared.config import Settings

__all__ = ["apply_configured_retention", "build", "build_ingest", "mount"]


def build(
    settings: Settings, *, engine: AsyncEngine, redis: Redis, devices: DevicesService
) -> TelemetryModule:
    return TelemetryModule(
        queries=TelemetryQueries(TimescaleReadings(engine), RedisLatestReadings(redis)),
        devices=devices,
        redis=redis,
        settings=settings,
    )


def build_ingest(
    settings: Settings, *, engine: AsyncEngine, redis: Redis, devices: DevicesService
) -> tuple[TelemetryIngest, TelemetryBuffer]:
    buffer = TelemetryBuffer(
        TimescaleReadings(engine).write,
        batch_rows=settings.telemetry_batch_rows,
        flush_interval_s=settings.telemetry_flush_interval_ms / 1000,
        max_rows=settings.telemetry_buffer_max_rows,
    )
    ingest = TelemetryIngest(
        buffer=buffer,
        latest=RedisLatestReadings(redis),
        dedup=RedisDeduplicator(redis),
        flood=RedisFloodGuard(redis, max_per_minute=settings.telemetry_max_messages_per_minute),
        devices=CachedDeviceDirectory(devices),
    )
    return ingest, buffer


async def apply_configured_retention(settings: Settings, engine: AsyncEngine) -> None:
    await apply_retention(
        engine,
        {
            "telemetry.readings": settings.telemetry_raw_retention_days,
            "telemetry.readings_1m": settings.telemetry_1m_retention_days,
            "telemetry.readings_1h": settings.telemetry_1h_retention_days,
        },
    )


def mount(app: FastAPI) -> None:
    app.include_router(routes.router)
    errors.register(app)
