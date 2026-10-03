"""Composition of the automations module for an entrypoint. Only entrypoints import this."""

from datetime import timedelta

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.automations.api import errors, routes
from smarthome.modules.automations.api.container import AutomationsModule
from smarthome.modules.automations.application.actions import ActionRunner
from smarthome.modules.automations.application.engine import AutomationEngine
from smarthome.modules.automations.application.services import AutomationsService
from smarthome.modules.automations.domain.engine import Limits
from smarthome.modules.automations.infrastructure.modules import (
    CommandsAdapter,
    DevicesAdapter,
    TelemetryHistoryAdapter,
)
from smarthome.modules.automations.infrastructure.persistence import (
    PostgresAutomationsUnitOfWork,
)
from smarthome.modules.automations.infrastructure.redis_stores import (
    CachedDirectory,
    RedisCausality,
)
from smarthome.modules.commands.public import CommandsService
from smarthome.modules.devices.public import DevicesService
from smarthome.modules.telemetry.public import TelemetryQueries
from smarthome.shared.clock import Clock
from smarthome.shared.config import Settings

__all__ = ["AutomationsModule", "build", "build_engine", "build_service", "mount"]

# Long enough for a command to reach its device and the device to report back.
CAUSALITY_TTL_S = 60


def _limits(settings: Settings) -> Limits:
    return Limits(
        max_chain_depth=settings.automation_max_chain_depth,
        max_runs_per_minute=settings.automation_max_runs_per_minute,
    )


def build_service(
    settings: Settings,
    *,
    engine: AsyncEngine,
    redis: Redis,
    devices: DevicesService,
    telemetry: TelemetryQueries,
    commands: CommandsService,
    clock: Clock,
) -> AutomationsService:
    def uow() -> PostgresAutomationsUnitOfWork:
        return PostgresAutomationsUnitOfWork(engine)

    return AutomationsService(
        uow,
        directory=CachedDirectory(redis, uow),
        devices=DevicesAdapter(devices, telemetry),
        history=TelemetryHistoryAdapter(telemetry),
        actions=ActionRunner(uow, CommandsAdapter(commands)),
        clock=clock,
        limits=_limits(settings),
    )


def build_engine(
    settings: Settings,
    *,
    engine: AsyncEngine,
    redis: Redis,
    devices: DevicesService,
    telemetry: TelemetryQueries,
    commands: CommandsService,
    clock: Clock,
) -> AutomationEngine:
    def uow() -> PostgresAutomationsUnitOfWork:
        return PostgresAutomationsUnitOfWork(engine)

    return AutomationEngine(
        uow,
        directory=CachedDirectory(redis, uow),
        devices=DevicesAdapter(devices, telemetry),
        actions=ActionRunner(uow, CommandsAdapter(commands)),
        causality=RedisCausality(redis, ttl_s=CAUSALITY_TTL_S),
        clock=clock,
        limits=_limits(settings),
        misfire_grace=timedelta(seconds=settings.automation_misfire_grace_s),
    )


def build(
    settings: Settings,
    *,
    engine: AsyncEngine,
    redis: Redis,
    devices: DevicesService,
    telemetry: TelemetryQueries,
    commands: CommandsService,
    clock: Clock,
) -> AutomationsModule:
    return AutomationsModule(
        service=build_service(
            settings,
            engine=engine,
            redis=redis,
            devices=devices,
            telemetry=telemetry,
            commands=commands,
            clock=clock,
        ),
        settings=settings,
        clock=clock,
    )


def mount(app: FastAPI) -> None:
    app.include_router(routes.router)
    errors.register(app)
