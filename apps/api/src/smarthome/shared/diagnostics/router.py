"""A deliberately small endpoint that exercises every telemetry signal.

One request produces a server span with Redis, Postgres and custom child spans,
a counter increment, a latency observation (with an exemplar pointing at the trace)
and a log line carrying the same trace id. Used by the README tour and to generate
traffic for the dashboards. Only mounted when `diagnostics_enabled` is set.
"""

import asyncio
from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from opentelemetry import metrics, trace
from pydantic import BaseModel
from sqlalchemy import text

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine

log = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

demo_runs = meter.create_counter(
    "smarthome.diagnostics.demo.runs",
    unit="{run}",
    description="Trace demo executions by outcome.",
)

HITS_KEY = "diagnostics:trace-demo:hits"
MAX_DELAY_MS = 2_000

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


class TraceDemoResponse(BaseModel):
    trace_id: str
    hits: int
    database_time: str


@router.get("/trace-demo")
async def trace_demo(
    request: Request,
    delay_ms: Annotated[int, Query(ge=0, le=MAX_DELAY_MS)] = 0,
    fail: bool = False,
) -> TraceDemoResponse:
    """Touch Redis and Postgres, optionally slow down or fail, and return the trace id."""
    redis: Redis = request.app.state.redis
    engine: AsyncEngine = request.app.state.db_engine
    trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")

    hits = int(await redis.incr(HITS_KEY))

    async with engine.connect() as conn:
        database_time = str(await conn.scalar(text("SELECT now()")))

    with tracer.start_as_current_span("diagnostics.simulated_work") as span:
        span.set_attribute("smarthome.demo.delay_ms", delay_ms)
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        if fail:
            span.set_status(trace.StatusCode.ERROR, "failure requested by caller")
            demo_runs.add(1, {"outcome": "failed"})
            log.warning("trace_demo_failed", hits=hits, delay_ms=delay_ms)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="simulated failure",
            )

    demo_runs.add(1, {"outcome": "succeeded"})
    log.info("trace_demo_completed", hits=hits, delay_ms=delay_ms)
    return TraceDemoResponse(trace_id=trace_id, hits=hits, database_time=database_time)
