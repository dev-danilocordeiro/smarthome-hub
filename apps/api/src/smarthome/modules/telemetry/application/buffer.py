"""Batches readings in memory and writes them in bulk.

Flushes when `batch_rows` are waiting or `flush_interval` elapsed, whichever comes first.
The buffer is bounded: when the database falls behind, `put` waits for room. That stalls
the MQTT consumer, which is the backpressure we want: the broker absorbs the slack (or
drops QoS 0 telemetry) instead of the ingestor growing without bound and dying OOM.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from opentelemetry import metrics

from smarthome.modules.telemetry.domain.model import Reading

log = structlog.get_logger(__name__)
meter = metrics.get_meter(__name__)

flush_duration = meter.create_histogram(
    "smarthome.telemetry.flush.duration", unit="s", description="Time to write one batch."
)
batch_size = meter.create_histogram(
    "smarthome.telemetry.batch.size", unit="{reading}", description="Readings per flush."
)
rows_written = meter.create_counter(
    "smarthome.telemetry.rows",
    unit="{reading}",
    description="Rows by outcome (written, duplicate).",
)
ingest_latency = meter.create_histogram(
    "smarthome.telemetry.ingest.latency",
    unit="s",
    description="Device timestamp to durable write (includes clock skew).",
)
flush_failures = meter.create_counter(
    "smarthome.telemetry.flush.failures",
    unit="{flush}",
    description="Failed batch writes (retried).",
)

Writer = Callable[[list[Reading]], Awaitable[int]]


class TelemetryBuffer:
    def __init__(
        self,
        writer: Writer,
        *,
        batch_rows: int,
        flush_interval_s: float,
        max_rows: int,
        retry_backoff_s: float = 1.0,
    ) -> None:
        self._writer = writer
        self._batch_rows = batch_rows
        self._flush_interval_s = flush_interval_s
        self._max_rows = max_rows
        self._retry_backoff_s = retry_backoff_s
        self._rows: list[Reading] = []
        self._room = asyncio.Condition()
        self._ready = asyncio.Event()
        meter.create_observable_gauge(
            "smarthome.telemetry.buffer.size",
            callbacks=[lambda _: [metrics.Observation(len(self._rows))]],
            unit="{reading}",
            description="Readings waiting to be written.",
        )

    def __len__(self) -> int:
        return len(self._rows)

    async def put(self, readings: list[Reading]) -> None:
        async with self._room:
            await self._room.wait_for(lambda: len(self._rows) + len(readings) <= self._max_rows)
            self._rows.extend(readings)
        if len(self._rows) >= self._batch_rows:
            self._ready.set()

    async def run(self, stop: asyncio.Event) -> None:
        """Flush loop. On stop, drains what is left before returning."""
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ready.wait(), timeout=self._flush_interval_s)
            self._ready.clear()
            await self.flush()
        while self._rows:
            await self.flush()

    async def flush(self) -> None:
        if not self._rows:
            return
        batch = self._rows[: self._batch_rows]
        started = time.perf_counter()
        try:
            written = await self._writer(batch)
        except Exception as exc:  # noqa: BLE001 - keep the rows and retry; never drop on a DB hiccup
            flush_failures.add(1)
            log.warning("telemetry_flush_failed", rows=len(batch), error=str(exc))
            await asyncio.sleep(self._retry_backoff_s)
            return
        duration = time.perf_counter() - started
        async with self._room:
            del self._rows[: len(batch)]
            self._room.notify_all()
        if len(self._rows) >= self._batch_rows:
            self._ready.set()

        flush_duration.record(duration)
        batch_size.record(len(batch))
        rows_written.add(written, {"outcome": "written"})
        rows_written.add(len(batch) - written, {"outcome": "duplicate"})
        now = datetime.now(UTC)
        for reading in batch[:: max(1, len(batch) // 20)]:  # sample: latency is a distribution
            ingest_latency.record(max(0.0, (now - reading.time).total_seconds()))
