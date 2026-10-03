import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from smarthome.modules.automations import wiring as automations
from smarthome.modules.commands import wiring as commands
from smarthome.modules.devices import wiring as devices
from smarthome.modules.energy import wiring as energy
from smarthome.modules.identity import wiring as identity
from smarthome.modules.notifications import wiring as notifications
from smarthome.modules.telemetry import wiring as telemetry
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings, get_settings
from smarthome.shared.diagnostics.router import router as diagnostics_router
from smarthome.shared.health.router import router as health_router
from smarthome.shared.http import preconditions
from smarthome.shared.http.security_headers import SecurityHeadersMiddleware
from smarthome.shared.infrastructure.db import create_engine
from smarthome.shared.infrastructure.event_stream import StreamTail
from smarthome.shared.infrastructure.redis import create_redis
from smarthome.shared.logging import configure_logging
from smarthome.shared.observability import (
    configure_providers,
    instrument_app,
    instrument_engine,
    instrument_process_metrics,
    instrument_redis,
)

log = structlog.get_logger(__name__)

LIVE_BLOCK_S = 1.0


def build_modules(app: FastAPI, settings: Settings) -> None:
    """Every module's HTTP-side composition, on `app.state` where its routes find it."""
    app.state.identity = identity.build(
        settings,
        engine=app.state.db_engine,
        redis=app.state.redis,
        http=app.state.http,
        clock=SystemClock(),
    )
    app.state.devices = devices.build(
        settings, engine=app.state.db_engine, redis=app.state.redis, clock=SystemClock()
    )
    app.state.telemetry = telemetry.build(
        settings,
        engine=app.state.db_engine,
        redis=app.state.redis,
        devices=app.state.devices.service,
    )
    app.state.commands = commands.build(
        settings,
        engine=app.state.db_engine,
        devices=app.state.devices.service,
        clock=SystemClock(),
    )
    app.state.automations = automations.build(
        settings,
        engine=app.state.db_engine,
        redis=app.state.redis,
        devices=app.state.devices.service,
        telemetry=app.state.telemetry.queries,
        commands=app.state.commands.service,
        clock=SystemClock(),
    )
    app.state.energy = energy.build(
        engine=app.state.db_engine, devices=app.state.devices.service, clock=SystemClock()
    )
    app.state.notifications = notifications.build(
        settings,
        engine=app.state.db_engine,
        identity=app.state.identity.service,
        clock=SystemClock(),
    )
    app.state.live = devices.build_live(
        settings, devices=app.state.devices.service, telemetry=app.state.telemetry.queries
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Composition root for the HTTP entrypoint.

    `ingestor` and `worker` have their own composition roots over the same modules
    (see docs/adr/0001-modular-monolith-with-multiple-entrypoints.md).
    """
    resolved = settings or get_settings()
    providers = configure_providers(resolved) if resolved.otel_enabled else None
    configure_logging(level=resolved.log_level, json=resolved.log_json, otlp=providers is not None)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        app.state.db_engine = create_engine(resolved)
        app.state.redis = create_redis(resolved)
        # One pooled client for outbound calls (OIDC provider today). Short timeouts: a
        # slow IdP must not pin request handlers.
        app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
        build_modules(app, resolved)
        # Its own connection: XREAD blocks. Every API replica tails the stream, so each
        # one can serve the live sockets of any home (ADR 0011).
        stream_redis = create_redis(resolved, socket_timeout=LIVE_BLOCK_S + 5)
        live_stop = asyncio.Event()
        live_tail = asyncio.create_task(
            StreamTail(
                stream_redis, app.state.live.hub.dispatch, block_ms=int(LIVE_BLOCK_S * 1000)
            ).run(live_stop)
        )
        instrument_engine(app.state.db_engine)
        instrument_redis()
        if providers is not None:
            instrument_process_metrics()
        log.info(
            "api_started",
            environment=resolved.environment.value,
            otel_enabled=resolved.otel_enabled,
            diagnostics_enabled=resolved.diagnostics_enabled,
        )
        try:
            yield
        finally:
            live_stop.set()
            await live_tail
            await stream_redis.aclose()
            await app.state.http.aclose()
            await app.state.redis.aclose()
            await app.state.db_engine.dispose()
            log.info("api_stopped")
            if providers is not None:
                providers.shutdown()

    app = FastAPI(title="Smart Home Hub API", version=resolved.service_version, lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(health_router)
    preconditions.register(app)
    identity.mount(app)
    devices.mount(app)
    telemetry.mount(app)
    commands.mount(app)
    automations.mount(app)
    energy.mount(app)
    notifications.mount(app)
    if resolved.diagnostics_enabled:
        app.include_router(diagnostics_router)
    instrument_app(app)
    return app
