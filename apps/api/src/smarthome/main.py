from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from smarthome.shared.config import Settings, get_settings
from smarthome.shared.health.router import router as health_router
from smarthome.shared.infrastructure.db import create_engine
from smarthome.shared.infrastructure.redis import create_redis
from smarthome.shared.logging import configure_logging

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Composition root for the HTTP entrypoint.

    `ingestor` and `worker` have their own composition roots over the same modules
    (see docs/adr/0001-modular-monolith-with-multiple-entrypoints.md).
    """
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, json=resolved.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        app.state.db_engine = create_engine(resolved)
        app.state.redis = create_redis(resolved)
        log.info("api_started", environment=resolved.environment.value)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.db_engine.dispose()
            log.info("api_stopped")

    app = FastAPI(title="Smart Home Hub API", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    return app
