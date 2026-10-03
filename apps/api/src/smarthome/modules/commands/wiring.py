"""Composition of the commands module for an entrypoint. Only entrypoints import this."""

from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.commands.api import errors, routes
from smarthome.modules.commands.api.container import CommandsModule
from smarthome.modules.commands.application.services import CommandsService
from smarthome.modules.commands.infrastructure.devices import DevicesModuleAdapter
from smarthome.modules.commands.infrastructure.persistence import PostgresCommandsUnitOfWork
from smarthome.modules.devices.public import DevicesService
from smarthome.shared.clock import Clock
from smarthome.shared.config import Settings
from smarthome.shared.events import EventPublisher

__all__ = ["CommandsModule", "build", "build_service", "mount"]


def build_service(
    settings: Settings,
    *,
    engine: AsyncEngine,
    devices: DevicesService,
    clock: Clock,
    events: EventPublisher | None = None,
) -> CommandsService:
    """`events`: where command outcomes are announced (ingestor and worker pass one)."""
    return CommandsService(
        lambda: PostgresCommandsUnitOfWork(engine),
        DevicesModuleAdapter(devices),
        clock,
        ack_grace=timedelta(seconds=settings.command_ack_grace_s),
        events=events,
    )


def build(
    settings: Settings, *, engine: AsyncEngine, devices: DevicesService, clock: Clock
) -> CommandsModule:
    return CommandsModule(
        service=build_service(settings, engine=engine, devices=devices, clock=clock),
        settings=settings,
        clock=clock,
    )


def mount(app: FastAPI) -> None:
    app.include_router(routes.router)
    errors.register(app)
