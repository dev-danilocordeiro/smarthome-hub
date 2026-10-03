"""Composition of the devices module for an entrypoint. Only entrypoints import this."""

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.devices.api import errors, live, routes
from smarthome.modules.devices.api.container import DevicesModule
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.devices.infrastructure.broker_admin import BrokerAdmin, BrokerConnection
from smarthome.modules.devices.infrastructure.live_state import RedisLiveState
from smarthome.modules.devices.infrastructure.persistence import PostgresDevicesUnitOfWork
from smarthome.modules.telemetry.public import TelemetryQueries
from smarthome.shared.clock import Clock
from smarthome.shared.config import Settings
from smarthome.shared.events import EventPublisher
from smarthome.shared.http.rate_limit import RateLimiter

__all__ = ["DevicesModule", "broker_admin", "build", "build_live", "build_service", "mount"]


def broker_admin(settings: Settings) -> BrokerAdmin:
    return BrokerAdmin(
        BrokerConnection(
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            ca_file=settings.mqtt_ca_file,
            username=settings.mqtt_admin_user,
            password=settings.mqtt_admin_password.get_secret_value(),
        )
    )


def build_service(
    settings: Settings,
    *,
    engine: AsyncEngine,
    redis: Redis,
    clock: Clock,
    events: EventPublisher | None = None,
) -> DevicesService:
    """`events`: only the ingestor records device traffic, so only it passes a publisher."""
    return DevicesService(
        lambda: PostgresDevicesUnitOfWork(engine),
        broker_admin(settings),
        RedisLiveState(redis),
        clock,
        events=events,
    )


def build(settings: Settings, *, engine: AsyncEngine, redis: Redis, clock: Clock) -> DevicesModule:
    return DevicesModule(
        service=build_service(settings, engine=engine, redis=redis, clock=clock),
        rate_limiter=RateLimiter(redis),
        settings=settings,
    )


def build_live(
    settings: Settings, *, devices: DevicesService, telemetry: TelemetryQueries
) -> live.LiveModule:
    """The live WebSocket's state; feed `module.hub.dispatch` from a `StreamTail`."""
    return live.LiveModule(
        hub=live.LiveHub(),
        devices=devices,
        telemetry=telemetry,
        recheck_every_s=settings.live_recheck_interval_s,
    )


def mount(app: FastAPI) -> None:
    app.include_router(routes.router)
    app.include_router(live.router)
    errors.register(app)
