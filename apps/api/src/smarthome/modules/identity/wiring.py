"""Composition of the identity module for an entrypoint. Only entrypoints import this."""

from datetime import timedelta

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.identity.api import bff, errors, homes
from smarthome.modules.identity.api.container import IdentityModule
from smarthome.modules.identity.application.services import IdentityService
from smarthome.modules.identity.infrastructure.oidc import OidcClient
from smarthome.modules.identity.infrastructure.persistence import PostgresUnitOfWork
from smarthome.modules.identity.infrastructure.sessions import RedisSessionStore, SessionManager
from smarthome.shared.clock import Clock
from smarthome.shared.config import Settings

__all__ = ["IdentityModule", "build", "mount"]


def build(
    settings: Settings, *, engine: AsyncEngine, redis: Redis, http: httpx.AsyncClient, clock: Clock
) -> IdentityModule:
    oidc = OidcClient(
        http=http,
        discovery_url=settings.oidc_discovery_url,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        clock=clock,
    )
    sessions = SessionManager(
        oidc=oidc,
        store=RedisSessionStore(redis, clock),
        clock=clock,
        absolute_lifetime=timedelta(hours=settings.session_absolute_lifetime_hours),
    )
    service = IdentityService(lambda: PostgresUnitOfWork(engine), clock)
    return IdentityModule(service=service, sessions=sessions, settings=settings, clock=clock)


def mount(app: FastAPI) -> None:
    app.include_router(bff.router)
    app.include_router(homes.router)
    errors.register(app)
