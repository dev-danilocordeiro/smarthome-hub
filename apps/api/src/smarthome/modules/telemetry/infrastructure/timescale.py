"""TimescaleDB adapters: bulk insert, resolution-aware queries, retention policy."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.telemetry.domain.model import Point, Reading, Resolution

# One round trip per batch: arrays in, rows out, duplicates skipped by the unique key.
INSERT_BATCH = text(
    "INSERT INTO telemetry.readings"
    " (time, home_id, device_id, metric, value, message_id, received_at)"
    " SELECT * FROM unnest("
    " CAST(:times AS timestamptz[]), CAST(:homes AS uuid[]), CAST(:devices AS text[]),"
    " CAST(:metrics AS text[]), CAST(:values AS double precision[]),"
    " CAST(:messages AS text[]), CAST(:received AS timestamptz[]))"
    " ON CONFLICT ON CONSTRAINT readings_once DO NOTHING"
)

RAW_SERIES = text(
    "SELECT time, value AS avg, value AS min, value AS max, 1 AS samples"
    " FROM telemetry.readings"
    " WHERE home_id = :home AND device_id = :device AND metric = :metric"
    " AND time >= :start AND time < :end ORDER BY time"
)
AGGREGATE_SERIES = {
    Resolution.MINUTE: text(
        "SELECT bucket AS time, total / samples AS avg, min, max, samples"
        " FROM telemetry.readings_1m"
        " WHERE home_id = :home AND device_id = :device AND metric = :metric"
        " AND bucket >= :start AND bucket < :end ORDER BY bucket"
    ),
    Resolution.HOUR: text(
        "SELECT bucket AS time, total / samples AS avg, min, max, samples"
        " FROM telemetry.readings_1h"
        " WHERE home_id = :home AND device_id = :device AND metric = :metric"
        " AND bucket >= :start AND bucket < :end ORDER BY bucket"
    ),
    Resolution.DAY: text(
        "SELECT bucket AS time, total / samples AS avg, min, max, samples"
        " FROM telemetry.readings_1d"
        " WHERE home_id = :home AND device_id = :device AND metric = :metric"
        " AND bucket >= :start AND bucket < :end ORDER BY bucket"
    ),
}

RETENTION_TARGETS = ("telemetry.readings", "telemetry.readings_1m", "telemetry.readings_1h")


class TimescaleReadings:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def write(self, readings: list[Reading]) -> int:
        if not readings:
            return 0
        async with self._engine.begin() as conn:
            result = await conn.execute(
                INSERT_BATCH,
                {
                    "times": [r.time for r in readings],
                    "homes": [r.home_id for r in readings],
                    "devices": [r.device_id for r in readings],
                    "metrics": [r.metric for r in readings],
                    "values": [r.value for r in readings],
                    "messages": [r.message_id for r in readings],
                    "received": [r.received_at for r in readings],
                },
            )
        return int(result.rowcount)

    async def series(
        self,
        *,
        home_id: UUID,
        device_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        resolution: Resolution,
    ) -> list[Point]:
        query = RAW_SERIES if resolution is Resolution.RAW else AGGREGATE_SERIES[resolution]
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                query,
                {
                    "home": home_id,
                    "device": device_id,
                    "metric": metric,
                    "start": start,
                    "end": end,
                },
            )
            return [
                Point(time=r.time, avg=r.avg, min=r.min, max=r.max, samples=int(r.samples))
                for r in rows
            ]


async def apply_retention(engine: AsyncEngine, days: dict[str, int]) -> None:
    """Make the retention policies match configuration (idempotent; run at startup)."""
    async with engine.begin() as conn:
        for relation in RETENTION_TARGETS:
            await conn.execute(
                text("SELECT remove_retention_policy(:rel, if_exists => true)"), {"rel": relation}
            )
            await conn.execute(
                text("SELECT add_retention_policy(:rel, make_interval(days => :days))"),
                {"rel": relation, "days": days[relation]},
            )
