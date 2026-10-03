import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from smarthome.modules.telemetry.application.buffer import TelemetryBuffer
from smarthome.modules.telemetry.application.services import TelemetryIngest, TelemetryQueries
from smarthome.modules.telemetry.domain.errors import InvalidRange
from smarthome.modules.telemetry.domain.model import (
    HourlyIncrease,
    Point,
    Reading,
    Resolution,
    choose_resolution,
    readings_from_message,
)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
HOME = uuid4()


def reading(i: int = 0) -> Reading:
    return Reading(T0 + timedelta(seconds=i), HOME, "d1", "power_w", float(i), f"m{i:08d}", T0)


def message(message_id: str = "msg-000001", **readings: Any) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "message_id": message_id,
        "seq": 1,
        "ts": T0.isoformat(),
        "readings": readings or {"power_w": 9.5},
    }


class RecordingWriter:
    def __init__(self) -> None:
        self.batches: list[list[Reading]] = []
        self.fail = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self, batch: list[Reading]) -> int:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            self.fail -= 1
            raise ConnectionError("db down")
        self.batches.append(list(batch))
        return len(batch)


def test_booleans_become_numbers_so_every_metric_aggregates_the_same_way() -> None:
    rows = readings_from_message(
        home_id=HOME,
        device_id="m1",
        payload=message(motion=True, illuminance_lux=12),
        received_at=T0,
    )

    assert {r.metric: r.value for r in rows} == {"motion": 1.0, "illuminance_lux": 12.0}
    assert {r.time for r in rows} == {T0}


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        (timedelta(minutes=30), Resolution.RAW),
        (timedelta(hours=2), Resolution.RAW),
        (timedelta(hours=3), Resolution.MINUTE),
        (timedelta(days=1), Resolution.MINUTE),
        (timedelta(days=7), Resolution.HOUR),
        (timedelta(days=60), Resolution.HOUR),
        (timedelta(days=365), Resolution.DAY),
    ],
)
def test_the_range_picks_the_coarsest_resolution_that_still_charts_well(
    span: timedelta, expected: Resolution
) -> None:
    assert choose_resolution(T0, T0 + span) is expected


async def test_a_full_batch_is_flushed_without_waiting_for_the_interval() -> None:
    writer = RecordingWriter()
    buffer = TelemetryBuffer(writer, batch_rows=3, flush_interval_s=60, max_rows=100)
    stop = asyncio.Event()
    task = asyncio.create_task(buffer.run(stop))

    await buffer.put([reading(i) for i in range(3)])
    await asyncio.sleep(0.05)

    assert [len(b) for b in writer.batches] == [3]
    stop.set()
    await task


async def test_a_partial_batch_is_flushed_when_the_interval_elapses() -> None:
    writer = RecordingWriter()
    buffer = TelemetryBuffer(writer, batch_rows=100, flush_interval_s=0.05, max_rows=1000)
    stop = asyncio.Event()
    task = asyncio.create_task(buffer.run(stop))

    await buffer.put([reading(1)])
    await asyncio.sleep(0.15)

    assert [len(b) for b in writer.batches] == [1]
    stop.set()
    await task


async def test_when_the_database_falls_behind_producers_wait_instead_of_growing_memory() -> None:
    writer = RecordingWriter()
    writer.gate = asyncio.Event()  # the database is stuck
    buffer = TelemetryBuffer(writer, batch_rows=2, flush_interval_s=0.01, max_rows=4)
    stop = asyncio.Event()
    task = asyncio.create_task(buffer.run(stop))
    await buffer.put([reading(i) for i in range(4)])

    blocked = asyncio.create_task(buffer.put([reading(9)]))
    await asyncio.sleep(0.1)
    assert not blocked.done()
    assert len(buffer) == 4

    writer.gate.set()
    await asyncio.wait_for(blocked, timeout=1)
    stop.set()
    await task
    assert sum(len(b) for b in writer.batches) == 5


async def test_a_failed_write_keeps_the_rows_and_retries_them() -> None:
    writer = RecordingWriter()
    writer.fail = 2
    buffer = TelemetryBuffer(
        writer, batch_rows=10, flush_interval_s=0.01, max_rows=100, retry_backoff_s=0.01
    )
    stop = asyncio.Event()
    task = asyncio.create_task(buffer.run(stop))

    await buffer.put([reading(1), reading(2)])
    await asyncio.sleep(0.2)
    stop.set()
    await task

    assert [len(b) for b in writer.batches] == [2]


async def test_stopping_drains_everything_still_buffered() -> None:
    writer = RecordingWriter()
    buffer = TelemetryBuffer(writer, batch_rows=2, flush_interval_s=60, max_rows=100)
    stop = asyncio.Event()
    stop.set()
    await buffer.put([reading(i) for i in range(5)])

    await buffer.run(stop)

    assert sum(len(b) for b in writer.batches) == 5
    assert len(buffer) == 0


@dataclass
class Fakes:
    active: bool = True
    over: bool = False
    seen: set[str] = field(default_factory=set)
    tripped: bool = False
    quarantined: list[str] = field(default_factory=list)
    latest: list[Reading] = field(default_factory=list)

    async def is_active(self, home_id: UUID, device_id: str) -> bool:
        return self.active

    async def quarantine(self, home_id: UUID, device_id: str, *, reason: str) -> None:
        self.quarantined.append(device_id)

    async def over_limit(self, device_id: str) -> bool:
        return self.over

    async def first_trip(self, device_id: str) -> bool:
        first, self.tripped = not self.tripped, True
        return first

    async def first_time(self, device_id: str, message_id: str) -> bool:
        if message_id in self.seen:
            return False
        self.seen.add(message_id)
        return True

    async def update(self, readings: list[Reading]) -> None:
        self.latest.extend(readings)

    async def get(self, device_id: str) -> dict[str, dict[str, object]]:
        return {}


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def ingest(fakes: Fakes) -> tuple[TelemetryIngest, RecordingWriter, TelemetryBuffer]:
    writer = RecordingWriter()
    buffer = TelemetryBuffer(writer, batch_rows=100, flush_interval_s=60, max_rows=1000)
    service = TelemetryIngest(buffer=buffer, latest=fakes, dedup=fakes, flood=fakes, devices=fakes)
    return service, writer, buffer


async def accept(service: TelemetryIngest, payload: dict[str, Any]) -> bool:
    return await service.accept(home_id=HOME, device_id="d1", payload=payload, received_at=T0)


async def test_an_accepted_message_is_buffered_and_shown_as_latest(
    ingest: tuple[TelemetryIngest, RecordingWriter, TelemetryBuffer], fakes: Fakes
) -> None:
    service, _, buffer = ingest

    assert await accept(service, message(power_w=5, voltage_v=127))
    assert len(buffer) == 2
    assert {r.metric for r in fakes.latest} == {"power_w", "voltage_v"}


async def test_a_redelivered_message_is_counted_once(
    ingest: tuple[TelemetryIngest, RecordingWriter, TelemetryBuffer],
) -> None:
    service, _, buffer = ingest

    assert await accept(service, message("dup-0000001"))
    assert not await accept(service, message("dup-0000001"))
    assert len(buffer) == 1


async def test_telemetry_from_an_unknown_or_inactive_device_is_dropped(
    ingest: tuple[TelemetryIngest, RecordingWriter, TelemetryBuffer], fakes: Fakes
) -> None:
    service, _, buffer = ingest
    fakes.active = False

    assert not await accept(service, message())
    assert len(buffer) == 0


async def test_a_flooding_device_is_quarantined_once_and_its_traffic_dropped(
    ingest: tuple[TelemetryIngest, RecordingWriter, TelemetryBuffer], fakes: Fakes
) -> None:
    service, _, buffer = ingest
    fakes.over = True

    results = [await accept(service, message(f"flood-{i:05d}")) for i in range(3)]

    assert results == [False, False, False]
    assert fakes.quarantined == ["d1"]
    assert len(buffer) == 0


class StubQueries:
    async def series(self, **kw: Any) -> list[Point]:
        self.kw = kw
        return []

    async def hourly_increase(self, **kw: Any) -> list[HourlyIncrease]:
        self.kw = kw
        return []


async def test_history_without_a_resolution_uses_the_range_and_rejects_inverted_ranges() -> None:
    stub = StubQueries()
    queries = TelemetryQueries(stub, Fakes())

    chosen, _ = await queries.series(
        home_id=HOME, device_id="d1", metric="power_w", start=T0, end=T0 + timedelta(days=3)
    )
    assert chosen is Resolution.HOUR
    with pytest.raises(InvalidRange):
        await queries.series(home_id=HOME, device_id="d1", metric="power_w", start=T0, end=T0)
