"""The device event stream over real Redis: a consumer group hands each event to one
worker, retries failures, rescues entries a dead worker left behind, and buries poison."""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from redis.asyncio import Redis

from smarthome.shared.events import DeviceEvent, DeviceEventKind
from smarthome.shared.infrastructure.event_stream import RedisEventPublisher, StreamConsumer
from tests.integration.helpers import eventually

HOME = uuid4()


def event(n: int) -> DeviceEvent:
    return DeviceEvent(
        HOME, f"dev-{n}", DeviceEventKind.TELEMETRY, datetime.now(UTC), {"power_w": float(n)}
    )


class Recorder:
    def __init__(self, *, fail_on: set[str] | None = None, failures: int = 1_000) -> None:
        self.handled: list[str] = []
        self.fail_on = fail_on or set()
        self.failures_left = failures
        self.attempts: Counter[str] = Counter()

    async def __call__(self, entry_id: str, received: DeviceEvent) -> None:
        self.attempts[received.device_id] += 1
        if received.device_id in self.fail_on and self.failures_left > 0:
            self.failures_left -= 1
            raise RuntimeError("database is down")
        self.handled.append(received.device_id)


def consumer(
    redis: Redis, stream: str, handler: Recorder, name: str = "w1", **kw: object
) -> StreamConsumer:
    return StreamConsumer(
        redis,
        stream=stream,
        group="automations",
        consumer=name,
        handler=handler,
        block_ms=100,
        retry_delay_s=0.05,
        **kw,  # type: ignore[arg-type]
    )


async def pending(redis: Redis, stream: str) -> int:
    summary = await redis.xpending(stream, "automations")
    return int(summary["pending"])


async def test_events_are_handled_in_order_and_acknowledged(
    redis: Redis, stream: str, publisher: RedisEventPublisher
) -> None:
    handler = Recorder()
    member = consumer(redis, stream, handler)
    await member.ensure_group()
    for n in range(5):
        await publisher.publish(event(n))

    stop = asyncio.Event()
    task = asyncio.create_task(member.run(stop))
    await eventually(lambda: _done(handler, 5))
    stop.set()
    await task

    assert handler.handled == [f"dev-{n}" for n in range(5)]
    assert await pending(redis, stream) == 0


async def _done(handler: Recorder, count: int) -> bool:
    return len(handler.handled) >= count


async def test_a_failing_event_is_retried_in_order_before_later_ones(
    redis: Redis, stream: str, publisher: RedisEventPublisher
) -> None:
    handler = Recorder(fail_on={"dev-1"}, failures=2)
    member = consumer(redis, stream, handler)
    await member.ensure_group()
    for n in range(3):
        await publisher.publish(event(n))

    stop = asyncio.Event()
    task = asyncio.create_task(member.run(stop))
    await eventually(lambda: _done(handler, 3))
    stop.set()
    await task

    assert handler.handled == ["dev-0", "dev-1", "dev-2"]
    assert handler.attempts["dev-1"] == 3
    assert await pending(redis, stream) == 0


async def test_an_event_that_keeps_failing_goes_to_the_dead_letter_stream(
    redis: Redis, stream: str, publisher: RedisEventPublisher
) -> None:
    handler = Recorder(fail_on={"dev-1"})
    member = consumer(redis, stream, handler, max_attempts=3)
    await member.ensure_group()
    for n in range(3):
        await publisher.publish(event(n))
    await redis.xadd(stream, {"garbage": "1"})  # undecodable

    stop = asyncio.Event()
    task = asyncio.create_task(member.run(stop))
    await eventually(lambda: _done(handler, 2))
    await eventually(lambda: _dead(redis, stream, 2))
    stop.set()
    await task

    assert handler.handled == ["dev-0", "dev-2"]
    assert handler.attempts["dev-1"] == 3
    dead: Any = await redis.xrange(f"{stream}:dead")
    assert [fields.get("device_id") for _, fields in dead] == ["dev-1", None]
    assert dead[0][1]["error"] == "RuntimeError: database is down"
    assert dead[1][1]["error"].startswith("undecodable")
    assert await pending(redis, stream) == 0


async def _dead(redis: Redis, stream: str, count: int) -> bool:
    return int(await redis.xlen(f"{stream}:dead")) >= count


async def test_entries_left_by_a_dead_worker_are_claimed_by_another(
    redis: Redis, stream: str, publisher: RedisEventPublisher
) -> None:
    crashed = consumer(redis, stream, Recorder(), name="crashed")
    await crashed.ensure_group()
    await publisher.publish(event(1))
    # "crashed" takes the entry and dies before acknowledging it.
    taken: Any = await redis.xreadgroup("automations", "crashed", {stream: ">"}, count=10)
    assert len(taken[0][1]) == 1

    survivor_handler = Recorder()
    survivor = consumer(redis, stream, survivor_handler, name="survivor", claim_idle_ms=200)
    stop = asyncio.Event()
    task = asyncio.create_task(survivor.run(stop))
    await eventually(lambda: _done(survivor_handler, 1))
    stop.set()
    await task

    assert survivor_handler.handled == ["dev-1"]
    assert await pending(redis, stream) == 0


async def test_workers_in_a_group_split_events_without_handling_one_twice(
    redis: Redis, stream: str, publisher: RedisEventPublisher
) -> None:
    handlers = [Recorder(), Recorder(), Recorder()]
    members = [consumer(redis, stream, h, name=f"w{i}") for i, h in enumerate(handlers)]
    await members[0].ensure_group()
    stop = asyncio.Event()
    tasks = [asyncio.create_task(m.run(stop)) for m in members]
    for n in range(300):
        await publisher.publish(event(n))

    async def all_handled() -> bool:
        return sum(len(h.handled) for h in handlers) >= 300

    await eventually(all_handled)
    stop.set()
    await asyncio.gather(*tasks)

    handled = [d for h in handlers for d in h.handled]
    assert sorted(handled) == sorted(f"dev-{n}" for n in range(300))
    assert sum(1 for h in handlers if h.handled) >= 2


async def test_the_trace_continues_from_the_publisher_to_the_handler(
    redis: Redis, stream: str, publisher: RedisEventPublisher, span_exporter: InMemorySpanExporter
) -> None:
    handler = Recorder()
    member = consumer(redis, stream, handler)
    await member.ensure_group()
    with trace.get_tracer(__name__).start_as_current_span("ingest device message") as span:
        await publisher.publish(event(1))
        trace_id = span.get_span_context().trace_id

    stop = asyncio.Event()
    task = asyncio.create_task(member.run(stop))
    await eventually(lambda: _done(handler, 1))
    stop.set()
    await task

    names = [s.name for s in span_exporter.get_finished_spans() if s.context.trace_id == trace_id]
    assert "automations handle device event" in names
