"""Postgres outbox (writer) and the relay that drains it to the broker (ADR 0009)."""

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog
from opentelemetry import metrics, propagate, trace
from opentelemetry.metrics import CallbackOptions, Observation
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from smarthome.shared.clock import Clock
from smarthome.shared.outbox.model import OutboxMessage

log = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

relayed_counter = meter.create_counter(
    "smarthome.outbox.relayed",
    unit="{message}",
    description="Outbox messages processed by the relay, by outcome (published, expired, failed).",
)
relay_lag = meter.create_histogram(
    "smarthome.outbox.relay.lag",
    unit="s",
    description="Time from commit to publication at the broker.",
)
# Last count seen by a relay in this process; reported by the gauge below.
_backlog = [0]


def _observe_backlog(_: CallbackOptions) -> list[Observation]:
    return [Observation(_backlog[0])]


meter.create_observable_gauge(
    "smarthome.outbox.backlog",
    callbacks=[_observe_backlog],
    unit="{message}",
    description="Outbox messages waiting to be published.",
)

NOTIFY_CHANNEL = "outbox"
MAX_RETRY_DELAY_S = 60
MAX_ERROR_LENGTH = 500

SELECT_DUE = text(
    "SELECT id, topic, qos, payload, headers, created_at, expires_at, attempts"
    " FROM outbox.messages"
    " WHERE processed_at IS NULL AND next_attempt_at <= :now"
    " ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED"
)
MARK_PROCESSED = text(
    "UPDATE outbox.messages SET outcome = :outcome, processed_at = :now,"
    " attempts = attempts + 1 WHERE id = :id"
)
MARK_FAILED = text(
    "UPDATE outbox.messages SET attempts = attempts + 1, last_error = :error,"
    " next_attempt_at = :retry_at WHERE id = :id"
)
COUNT_BACKLOG = text("SELECT count(*) FROM outbox.messages WHERE processed_at IS NULL")


class PostgresOutbox:
    """Appends on the caller's connection, so the message commits or rolls back together
    with the change that produced it. The current trace context travels in the headers,
    so the relay's publish span continues the trace of the request that caused it."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._conn = connection

    async def add(self, message: OutboxMessage, *, created_at: datetime) -> None:
        headers = dict(message.headers)
        propagate.inject(headers)
        await self._conn.execute(
            text(
                "INSERT INTO outbox.messages (topic, qos, payload, headers, created_at,"
                " expires_at, next_attempt_at)"
                " VALUES (:topic, :qos, CAST(:payload AS jsonb), CAST(:headers AS jsonb),"
                " :created_at, :expires_at, :created_at)"
            ),
            {
                "topic": message.topic,
                "qos": message.qos,
                "payload": json.dumps(message.payload),
                "headers": json.dumps(headers),
                "created_at": created_at,
                "expires_at": message.expires_at,
            },
        )


class Publisher(Protocol):
    async def publish(
        self, topic: str, payload: bytes, *, qos: int, headers: Mapping[str, str]
    ) -> None:
        """Return once the broker has taken the message (PUBACK for QoS 1)."""
        ...


@dataclass(frozen=True, slots=True)
class RelayResult:
    published: int = 0
    expired: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        return self.published + self.expired + self.failed


class OutboxRelay:
    """Publishes committed outbox messages, oldest first, at least once.

    Rows are claimed with `FOR UPDATE SKIP LOCKED`, so several relays can run side by
    side without publishing the same row concurrently. A row is marked only after the
    broker acknowledged it: a crash in between republishes it, which consumers absorb
    (devices execute a command id at most once).
    """

    def __init__(
        self,
        engine: AsyncEngine,
        publisher: Publisher,
        clock: Clock,
        *,
        batch_size: int = 100,
        poll_interval_s: float = 1.0,
        listen: bool = True,
    ) -> None:
        self._engine = engine
        self._publisher = publisher
        self._clock = clock
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._notifications = listen
        self._wake = asyncio.Event()

    async def run(self, stop: asyncio.Event) -> None:
        listener = asyncio.create_task(self._listen_loop(stop)) if self._notifications else None
        try:
            while not stop.is_set():
                try:
                    result = await self.relay_once()
                except Exception:
                    log.exception("outbox_relay_failed")
                    result = RelayResult()
                if result.processed == self._batch_size and not result.failed:
                    continue  # more is waiting; drain without sleeping
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(_any(self._wake, stop), timeout=self._poll_interval_s)
        finally:
            if listener is not None:
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener

    async def relay_once(self) -> RelayResult:
        published = expired = failed = 0
        async with self._engine.connect() as conn, conn.begin():
            now = self._clock.now()
            rows = (await conn.execute(SELECT_DUE, {"now": now, "limit": self._batch_size})).all()
            for row in rows:
                if row.expires_at is not None and row.expires_at <= now:
                    await conn.execute(
                        MARK_PROCESSED, {"id": row.id, "outcome": "expired", "now": now}
                    )
                    expired += 1
                    continue
                try:
                    await self._publish(row)
                except Exception as exc:  # noqa: BLE001 - recorded on the row and retried
                    delay = min(2**row.attempts, MAX_RETRY_DELAY_S)
                    await conn.execute(
                        MARK_FAILED,
                        {
                            "id": row.id,
                            "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH],
                            "retry_at": now + timedelta(seconds=delay),
                        },
                    )
                    log.warning("outbox_publish_failed", message_id=row.id, error=str(exc))
                    failed += 1
                    break  # the broker is likely down: keep order, retry the batch later
                published_at = self._clock.now()
                await conn.execute(
                    MARK_PROCESSED, {"id": row.id, "outcome": "published", "now": published_at}
                )
                relay_lag.record((published_at - row.created_at).total_seconds())
                published += 1
            _backlog[0] = int(await conn.scalar(COUNT_BACKLOG) or 0)
        for outcome, count in (("published", published), ("expired", expired), ("failed", failed)):
            if count:
                relayed_counter.add(count, {"outcome": outcome})
        return RelayResult(published, expired, failed)

    async def _publish(self, row: Any) -> None:
        headers: dict[str, str] = dict(row.headers)
        parent = propagate.extract(headers)
        with tracer.start_as_current_span(
            "outbox publish",
            context=parent,
            kind=trace.SpanKind.PRODUCER,
            attributes={
                "messaging.system": "mqtt",
                "messaging.operation.type": "send",
                "messaging.destination.name": row.topic,
                "smarthome.outbox.message_id": row.id,
                "smarthome.outbox.attempt": row.attempts + 1,
            },
        ):
            # Downstream (the device) continues from this span, not from the request.
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            payload = json.dumps(row.payload, separators=(",", ":"), ensure_ascii=False)
            await self._publisher.publish(row.topic, payload.encode(), qos=row.qos, headers=carrier)

    async def purge_processed(self, *, older_than: timedelta) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("DELETE FROM outbox.messages WHERE processed_at < :cutoff"),
                {"cutoff": self._clock.now() - older_than},
            )
        return int(result.rowcount)

    async def _listen_loop(self, stop: asyncio.Event) -> None:
        """LISTEN on a dedicated connection; every NOTIFY wakes the relay at once.
        Losing the connection only costs latency: the poll interval still applies."""
        while not stop.is_set():
            started = time.monotonic()
            try:
                await self._listen(stop)
            except Exception:
                log.warning("outbox_listen_lost", exc_info=True)
            # Do not spin if the database refuses connections.
            await asyncio.sleep(max(0.0, 5 - (time.monotonic() - started)))

    async def _listen(self, stop: asyncio.Event) -> None:
        async with self._engine.connect() as conn:
            raw = await conn.get_raw_connection()
            driver = raw.driver_connection
            if driver is None:  # pragma: no cover - asyncpg always provides one
                return
            lost = asyncio.Event()

            def wake(*_: object) -> None:
                self._wake.set()

            driver.add_termination_listener(lambda *_: lost.set())
            await driver.add_listener(NOTIFY_CHANNEL, wake)
            try:
                await _any(stop, lost)
            finally:
                if not driver.is_closed():
                    await driver.remove_listener(NOTIFY_CHANNEL, wake)


async def _any(*events: asyncio.Event) -> None:
    waiters = {asyncio.ensure_future(e.wait()) for e in events}
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
