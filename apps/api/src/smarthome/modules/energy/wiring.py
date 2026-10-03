"""Composition of the energy module for an entrypoint. Only entrypoints import this."""

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.devices.public import DevicesService
from smarthome.modules.energy.api import errors, routes
from smarthome.modules.energy.api.routes import EnergyModule
from smarthome.modules.energy.application.services import EnergyRollup, EnergyService
from smarthome.modules.energy.infrastructure.modules import MeteredDevices, TelemetryCounters
from smarthome.modules.energy.infrastructure.persistence import PostgresEnergyUnitOfWork
from smarthome.modules.telemetry.public import TelemetryQueries
from smarthome.shared.clock import Clock

__all__ = ["EnergyModule", "build", "build_rollup", "build_service", "mount"]


def build_service(*, engine: AsyncEngine, devices: DevicesService, clock: Clock) -> EnergyService:
    return EnergyService(lambda: PostgresEnergyUnitOfWork(engine), MeteredDevices(devices), clock)


def build_rollup(*, engine: AsyncEngine, telemetry: TelemetryQueries, clock: Clock) -> EnergyRollup:
    return EnergyRollup(
        lambda: PostgresEnergyUnitOfWork(engine), TelemetryCounters(telemetry), clock
    )


def build(*, engine: AsyncEngine, devices: DevicesService, clock: Clock) -> EnergyModule:
    return EnergyModule(service=build_service(engine=engine, devices=devices, clock=clock))


def mount(app: FastAPI) -> None:
    app.include_router(routes.router)
    errors.register(app)
