"""Device events over a Redis stream: publisher (ingestor) and consumer group (worker).

ADR 0010. The stream is a buffer between "a device changed" and "react to it", not a
system of record: producers write after their own commit, consumers acknowledge after
their own commit, and a consumer group lets worker replicas split the stream.
"""

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from opentelemetry import metrics, propagate, trace
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from smarthome.shared.events import DeviceEvent, DeviceEventKind

log = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

published_counter = meter.create_counter(
    "smarthome.events.published",
    unit="{event}",
    description="Device events written to the stream, by kind and outcome (ok, failed).",
)
consumed_counter = meter.create_counter(
    "smarthome.events.consumed",
    unit="{event}",
    description="Device events handled by a consumer group, by group and outcome.",
)
consume_lag = meter.create_histogram(
    "smarthome.events.lag",
    unit="s",
    description="Time from writing an event to the stream to handling it.",
)

DEVICE_EVENTS = "events:devices"
DEAD_LETTER_MAXLEN = 10_000

# (stream entry id, event) -> None. Raising leaves the entry pending for a retry.
EventHandler = Callable[[str, DeviceEvent], Awaitable[object]]


class InvalidEvent(ValueError):
    pass


class HandlerFailed(RuntimeError):
    """A handler raised; its entry stays pending and is retried after a pause."""


def encode(event: DeviceEvent) -> dict[str, str]:
    headers: dict[str, str] = {}
    propagate.inject(headers)
    return {
        "home_id": str(event.home_id),
        "device_id": event.device_id,
        "kind": event.kind.value,
        "at": event.at.isoformat(),
        "data": json.dumps(event.data, separators=(",", ":")),
        "headers": json.dumps(headers),
    }


def decode(fields: dict[str, str]) -> tuple[DeviceEvent, dict[str, str]]:
    try:
        event = DeviceEvent(
            home_id=UUID(fields["home_id"]),
            device_id=fields["device_id"],
            kind=DeviceEventKind(fields["kind"]),
            at=datetime.fromisoformat(fields["at"]),
            data=json.loads(fields["data"]),
        )
        headers = json.loads(fields.get("headers") or "{}")
    except (KeyError, ValueError) as exc:
        raise InvalidEvent(str(exc)) from exc
    if event.at.tzinfo is None or not isinstance(event.data, dict):
        raise InvalidEvent("naive timestamp or non-object data")
    return event, {str(k): str(v) for k, v in headers.items()}


def entry_age_s(entry_id: str, *, now: float | None = None) -> float:
    """Stream ids start with the millisecond the entry was added."""
    millis = int(entry_id.split("-", 1)[0])
    return max(0.0, (now if now is not None else time.time()) - millis / 1000)


class RedisEventPublisher:
    def __init__(self, redis: Redis, *, stream: str = DEVICE_EVENTS, maxlen: int) -> None:
        self._redis = redis
        self._stream = stream
        self._maxlen = maxlen

    async def publish(self, event: DeviceEvent) -> None:
        try:
            await self._redis.xadd(
                self._stream,
                encode(event),  # type: ignore[arg-type]
                maxlen=self._maxlen,
                approximate=True,
            )
        except (RedisError, OSError):
            # The change itself is committed; only reactions to it are lost (ADR 0010).
            log.warning("device_event_not_published", device_id=event.device_id, exc_info=True)
            published_counter.add(1, {"kind": event.kind.value, "outcome": "failed"})
            return
        published_counter.add(1, {"kind": event.kind.value, "outcome": "ok"})


class StreamConsumer:
    """One member of a consumer group.

    - New entries are read with `>`; each is acknowledged only after the handler returns.
    - On start, and after any failure, the consumer first re-reads its own pending
      entries (id `0`), so nothing it was given is skipped.
    - Entries left pending by a consumer that died are claimed after `claim_idle_ms`.
    - An entry that fails `max_attempts` times, or cannot be decoded, goes to the dead
      letter stream instead of blocking the group forever.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        group: str,
        consumer: str,
        handler: EventHandler,
        stream: str = DEVICE_EVENTS,
        batch_size: int = 100,
        block_ms: int = 1000,
        claim_idle_ms: int = 60_000,
        max_attempts: int = 5,
        retry_delay_s: float = 1.0,
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._dead_letter = f"{stream}:dead"
        self._group = group
        self._consumer = consumer
        self._handler = handler
        self._batch = batch_size
        self._block_ms = block_ms
        self._claim_idle_ms = claim_idle_ms
        self._max_attempts = max_attempts
        self._retry_delay_s = retry_delay_s
        self._attempts: dict[str, int] = {}
        self._last_claim = 0.0

    async def ensure_group(self) -> None:
        """Create the group at the end of the stream: events from before the first
        worker ever started are not replayed."""
        try:
            await self._redis.xgroup_create(self._stream, self._group, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def run(self, stop: asyncio.Event) -> None:
        pending_first = True
        while not stop.is_set():
            try:
                await self.ensure_group()
                while not stop.is_set():
                    if pending_first:
                        pending_first = await self.drain_pending()
                    else:
                        await self.consume_once()
            except (RedisError, OSError, HandlerFailed) as exc:
                log.warning("event_consumer_retrying", group=self._group, error=str(exc))
                pending_first = True
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._retry_delay_s)

    async def drain_pending(self) -> bool:
        """Handle this consumer's own unacknowledged entries; True while some remain."""
        entries = await self._read("0", block=False)
        await self._handle_all(entries)
        return bool(entries)

    async def consume_once(self) -> int:
        """Claim abandoned entries (now and then), then read new ones. Returns how many
        entries were handled."""
        handled = 0
        if time.monotonic() - self._last_claim >= self._claim_idle_ms / 1000:
            self._last_claim = time.monotonic()
            handled += await self.claim_abandoned()
        entries = await self._read(">", block=True)
        await self._handle_all(entries)
        return handled + len(entries)

    async def claim_abandoned(self) -> int:
        result = await self._redis.xautoclaim(
            self._stream,
            self._group,
            self._consumer,
            min_idle_time=self._claim_idle_ms,
            start_id="0-0",
            count=self._batch,
        )
        # [next start id, claimed entries, ids no longer in the stream (Redis 7+)]
        _, claimed, *rest = result
        deleted: list[str] = rest[0] if rest else []
        if deleted:  # trimmed from the stream before anyone handled them
            await self._redis.xack(self._stream, self._group, *deleted)
        if claimed:
            log.info("event_entries_claimed", group=self._group, count=len(claimed))
        await self._handle_all(claimed)
        return len(claimed)

    async def _read(self, start: str, *, block: bool) -> list[tuple[str, dict[str, str]]]:
        response: Any = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: start},
            count=self._batch,
            block=self._block_ms if block else None,
        )
        if not response:
            return []
        entries: list[tuple[str, dict[str, str]]] = response[0][1]
        # Pending entries whose data was trimmed come back with no fields.
        gone = [entry_id for entry_id, fields in entries if not fields]
        if gone:
            await self._redis.xack(self._stream, self._group, *gone)
        return [(entry_id, fields) for entry_id, fields in entries if fields]

    async def _handle_all(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        for entry_id, fields in entries:
            await self._handle(entry_id, fields)

    async def _handle(self, entry_id: str, fields: dict[str, str]) -> None:
        try:
            event, headers = decode(fields)
        except InvalidEvent as exc:
            await self._bury(entry_id, fields, f"undecodable: {exc}")
            return
        attributes = {"group": self._group}
        with tracer.start_as_current_span(
            f"{self._group} handle device event",
            context=propagate.extract(headers) if headers else None,
            kind=trace.SpanKind.CONSUMER,
            attributes={
                "messaging.system": "redis",
                "messaging.destination.name": self._stream,
                "messaging.message.id": entry_id,
                "smarthome.device.id": event.device_id,
                "smarthome.event.kind": event.kind.value,
            },
        ) as span:
            try:
                await self._handler(entry_id, event)
            except Exception as exc:
                attempts = self._attempts.get(entry_id, 0) + 1
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, "handler failed")
                if attempts >= self._max_attempts:
                    self._attempts.pop(entry_id, None)
                    log.exception("device_event_dead_lettered", entry_id=entry_id)
                    await self._bury(entry_id, fields, f"{type(exc).__name__}: {exc}"[:500])
                    return
                self._attempts[entry_id] = attempts
                consumed_counter.add(1, attributes | {"outcome": "retry"})
                # Stop here: the rest of the batch is re-read after this one, in order.
                raise HandlerFailed(f"{entry_id}: {exc}") from exc
        self._attempts.pop(entry_id, None)
        await self._redis.xack(self._stream, self._group, entry_id)
        consumed_counter.add(1, attributes | {"outcome": "ok"})
        consume_lag.record(entry_age_s(entry_id), attributes)

    async def _bury(self, entry_id: str, fields: dict[str, Any], error: str) -> None:
        entry: dict[Any, Any] = {**fields, "entry_id": entry_id, "group": self._group}
        await self._redis.xadd(
            self._dead_letter,
            entry | {"error": error},
            maxlen=DEAD_LETTER_MAXLEN,
            approximate=True,
        )
        await self._redis.xack(self._stream, self._group, entry_id)
        consumed_counter.add(1, {"group": self._group, "outcome": "dead_lettered"})
