from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from smarthome.modules.devices import wiring as devices
from smarthome.modules.identity import wiring as identity
from smarthome.modules.telemetry import wiring as telemetry
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings, get_settings
from smarthome.shared.diagnostics.router import router as diagnostics_router
from smarthome.shared.health.router import router as health_router
from smarthome.shared.http.security_headers import SecurityHeadersMiddleware
from smarthome.shared.infrastructure.db import create_engine
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
        app.state.identity = identity.build(
            resolved,
            engine=app.state.db_engine,
            redis=app.state.redis,
            http=app.state.http,
            clock=SystemClock(),
        )
        app.state.devices = devices.build(
            resolved, engine=app.state.db_engine, redis=app.state.redis, clock=SystemClock()
        )
        app.state.telemetry = telemetry.build(
            resolved,
            engine=app.state.db_engine,
            redis=app.state.redis,
            devices=app.state.devices.service,
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
            await app.state.http.aclose()
            await app.state.redis.aclose()
            await app.state.db_engine.dispose()
            log.info("api_stopped")
            if providers is not None:
                providers.shutdown()

    app = FastAPI(title="Smart Home Hub API", version=resolved.service_version, lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(health_router)
    identity.mount(app)
    devices.mount(app)
    telemetry.mount(app)
    if resolved.diagnostics_enabled:
        app.include_router(diagnostics_router)
    instrument_app(app)
    return app
