from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from smarthome.shared.config import Settings, get_settings
from smarthome.shared.diagnostics.router import router as diagnostics_router
from smarthome.shared.health.router import router as health_router
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
            await app.state.redis.aclose()
            await app.state.db_engine.dispose()
            log.info("api_stopped")
            if providers is not None:
                providers.shutdown()

    app = FastAPI(title="Smart Home Hub API", version=resolved.service_version, lifespan=lifespan)
    app.include_router(health_router)
    if resolved.diagnostics_enabled:
        app.include_router(diagnostics_router)
    instrument_app(app)
    return app
