import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

CheckStatus = Literal["up", "down"]


class LivenessResponse(BaseModel):
    status: Literal["up"] = "up"


class ReadinessResponse(BaseModel):
    status: CheckStatus
    checks: dict[str, CheckStatus]


@router.get("/live")
async def live() -> LivenessResponse:
    """The process is running and serving requests. Never touches dependencies."""
    return LivenessResponse()


@router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(request: Request, response: Response) -> ReadinessResponse:
    """Every dependency the API needs to serve traffic is reachable."""
    engine: AsyncEngine = request.app.state.db_engine
    redis: Redis = request.app.state.redis
    timeout: float = request.app.state.settings.readiness_timeout_seconds

    async def check_postgres() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def check_redis() -> None:
        await redis.ping()

    probes: dict[str, Callable[[], Awaitable[None]]] = {
        "postgres": check_postgres,
        "redis": check_redis,
    }
    results = await asyncio.gather(*(_probe(name, fn, timeout) for name, fn in probes.items()))
    checks = dict(zip(probes, results, strict=True))

    overall: CheckStatus = "up" if all(r == "up" for r in checks.values()) else "down"
    if overall == "down":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status=overall, checks=checks)


async def _probe(
    name: str, fn: Callable[[], Awaitable[None]], budget_seconds: float
) -> CheckStatus:
    try:
        async with asyncio.timeout(budget_seconds):
            await fn()
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"; details go to the log
        log.warning("readiness_probe_failed", dependency=name, error_type=type(exc).__name__)
        return "down"
    return "up"
