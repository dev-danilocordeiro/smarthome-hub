"""The automation engine over real Postgres and Redis: from a device report to a queued
command, the races its locks exist for, a real loop being stopped, and schema guards."""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.automations.application.engine import AutomationEngine
from smarthome.modules.automations.application.services import AutomationsService
from smarthome.modules.automations.domain.model import Automation, AutomationStatus, RunStatus
from smarthome.modules.commands.application.services import CommandsService
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.telemetry import wiring as telemetry
from smarthome.modules.telemetry.application.services import TelemetryQueries
from smarthome.shared.config import Settings
from smarthome.shared.events import DeviceEvent, DeviceEventKind
from smarthome.shared.infrastructure.event_stream import RedisEventPublisher, StreamConsumer
from tests.integration.automations.conftest import Household, build_engine
from tests.integration.helpers import eventually

RACE_ROUNDS = 10


def command(device: str, **desired: Any) -> dict[str, Any]:
    return {"type": "command", "device_id": device, "action": "set_state", "desired": desired}


async def create(
    service: AutomationsService, household: Household, definition: dict[str, Any], name: str = ""
) -> Automation:
    saved = await service.create(
        home_id=household.home,
        timezone="America/Sao_Paulo",
        name=name or f"automation {secrets.token_hex(3)}",
        description=None,
        enabled=True,
        definition={"schema_version": "1"} | definition,
        actor=household.owner.principal.user_id,
    )
    return saved.automation


async def runs(engine: AsyncEngine, automation_id: UUID) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT status, event_key FROM automations.runs WHERE automation_id = :id"
                " ORDER BY started_at"
            ),
            {"id": automation_id},
        )
        return [(r.status, r.event_key) for r in rows]


async def commands_by(engine: AsyncEngine, actor: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT device_id, desired, trace_id FROM commands.commands WHERE issued_by = :by"
                " ORDER BY issued_at"
            ),
            {"by": actor},
        )
        return [dict(r._mapping) for r in rows]


async def test_a_sensor_reading_turns_into_a_queued_command_in_one_trace(
    *,
    migrated_database: Settings,
    engine: AsyncEngine,
    redis: Redis,
    stream: str,
    household: Household,
    device_service: DevicesService,
    publisher: RedisEventPublisher,
    automation_service: AutomationsService,
    automation_engine: AutomationEngine,
    span_exporter: InMemorySpanExporter,
) -> None:
    motion, hall = household.devices["motion"], household.devices["hall"]
    automation = await create(
        automation_service,
        household,
        {
            "triggers": [
                {
                    "type": "telemetry",
                    "device_id": motion,
                    "metric": "motion",
                    "op": "eq",
                    "value": True,
                }
            ],
            "actions": [command(hall, on=True, brightness_pct=70)],
        },
    )
    ingest, buffer = telemetry.build_ingest(
        migrated_database, engine=engine, redis=redis, devices=device_service, events=publisher
    )
    consumer = StreamConsumer(
        redis,
        stream=stream,
        group="automations",
        consumer="w1",
        handler=automation_engine.handle_event,
        block_ms=100,
    )
    await consumer.ensure_group()
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))

    def reading(value: bool, seconds: int) -> dict[str, Any]:
        ts = datetime(2026, 10, 5, 12, 0, seconds, tzinfo=UTC).isoformat()
        return {
            "schema_version": "1",
            "message_id": secrets.token_hex(8),
            "seq": seconds,
            "ts": ts,
            "readings": {"motion": value},
        }

    tracer = trace.get_tracer(__name__)
    for value, second in ((False, 0), (True, 1)):
        with tracer.start_as_current_span("ingest device message") as span:
            await ingest.accept(
                home_id=household.home,
                device_id=motion,
                payload=reading(value, second),
                received_at=datetime.now(UTC),
            )
            trace_id = format(span.get_span_context().trace_id, "032x")

    actor = f"automation:{automation.id}"

    async def queued() -> bool:
        return len(await commands_by(engine, actor)) == 1

    await eventually(queued)
    stop.set()
    await task
    await buffer.flush()

    [sent] = await commands_by(engine, actor)
    assert sent["device_id"] == hall
    assert sent["desired"] == {"on": True, "brightness_pct": 70}
    assert sent["trace_id"] == trace_id  # reading -> engine -> command, one trace
    assert [s for s, _ in await runs(engine, automation.id)] == ["completed"]
    names = {
        s.name
        for s in span_exporter.get_finished_spans()
        if format(s.context.trace_id, "032x") == trace_id
    }
    assert {"automations handle device event", "automation run"} <= names


async def test_one_event_delivered_to_two_workers_runs_the_automation_once(
    *,
    migrated_database: Settings,
    engine: AsyncEngine,
    redis: Redis,
    household: Household,
    device_service: DevicesService,
    queries: TelemetryQueries,
    command_service: CommandsService,
    automation_service: AutomationsService,
) -> None:
    motion, hall = household.devices["motion"], household.devices["hall"]
    automation = await create(
        automation_service,
        household,
        {
            "triggers": [
                {
                    "type": "telemetry",
                    "device_id": motion,
                    "metric": "motion",
                    "op": "eq",
                    "value": True,
                }
            ],
            "actions": [command(hall, on=True)],
        },
    )
    workers = [
        build_engine(
            settings=migrated_database,
            engine=engine,
            redis=redis,
            devices=device_service,
            queries=queries,
            commands_=command_service,
        )
        for _ in range(2)
    ]
    t0 = datetime.now(UTC)
    for round_ in range(RACE_ROUNDS):
        off = DeviceEvent(
            household.home,
            motion,
            DeviceEventKind.TELEMETRY,
            t0 + timedelta(seconds=2 * round_),
            {"motion": False},
        )
        on = DeviceEvent(
            household.home,
            motion,
            DeviceEventKind.TELEMETRY,
            t0 + timedelta(seconds=2 * round_ + 1),
            {"motion": True},
        )
        await workers[0].handle_event(f"{round_}-0", off)
        # A redelivery (claimed from a slow worker) racing the original.
        await asyncio.gather(*(w.handle_event(f"{round_}-1", on) for w in workers))

    acted = [s for s, _ in await runs(engine, automation.id) if s != "skipped"]
    assert len(acted) == RACE_ROUNDS
    assert len(await commands_by(engine, f"automation:{automation.id}")) == RACE_ROUNDS


async def test_concurrent_triggers_respect_the_cooldown(
    *,
    migrated_database: Settings,
    engine: AsyncEngine,
    redis: Redis,
    household: Household,
    device_service: DevicesService,
    queries: TelemetryQueries,
    command_service: CommandsService,
    automation_service: AutomationsService,
) -> None:
    """Two different events firing two triggers of one automation at the same moment, on
    two workers. Without the automation row lock both would see "no recent run" and both
    would act."""
    motion, climate, hall = (household.devices[k] for k in ("motion", "climate", "hall"))
    workers = [
        build_engine(
            settings=migrated_database,
            engine=engine,
            redis=redis,
            devices=device_service,
            queries=queries,
            commands_=command_service,
        )
        for _ in range(2)
    ]
    for round_ in range(RACE_ROUNDS):
        automation = await create(
            automation_service,
            household,
            {
                "triggers": [
                    {
                        "type": "telemetry",
                        "device_id": motion,
                        "metric": "motion",
                        "op": "eq",
                        "value": True,
                    },
                    {
                        "type": "telemetry",
                        "device_id": climate,
                        "metric": "temperature_c",
                        "op": "gt",
                        "value": 26,
                    },
                ],
                "actions": [command(hall, on=True)],
                "cooldown_s": 3600,
            },
        )
        t0 = datetime.now(UTC)
        await workers[0].handle_event(
            f"{round_}-a",
            DeviceEvent(household.home, motion, DeviceEventKind.TELEMETRY, t0, {"motion": False}),
        )
        await workers[0].handle_event(
            f"{round_}-b",
            DeviceEvent(
                household.home, climate, DeviceEventKind.TELEMETRY, t0, {"temperature_c": 20.0}
            ),
        )
        later = t0 + timedelta(seconds=1)
        await asyncio.gather(
            workers[0].handle_event(
                f"{round_}-c",
                DeviceEvent(
                    household.home, motion, DeviceEventKind.TELEMETRY, later, {"motion": True}
                ),
            ),
            workers[1].handle_event(
                f"{round_}-d",
                DeviceEvent(
                    household.home,
                    climate,
                    DeviceEventKind.TELEMETRY,
                    later,
                    {"temperature_c": 30.0},
                ),
            ),
        )

        statuses = sorted(s for s, _ in await runs(engine, automation.id))
        assert statuses == ["completed", "suppressed"], f"round {round_}"


async def test_two_workers_sweeping_timers_fire_each_hold_once(
    *,
    migrated_database: Settings,
    engine: AsyncEngine,
    redis: Redis,
    household: Household,
    device_service: DevicesService,
    queries: TelemetryQueries,
    command_service: CommandsService,
    automation_service: AutomationsService,
) -> None:
    motion, hall = household.devices["motion"], household.devices["hall"]
    workers = [
        build_engine(
            settings=migrated_database,
            engine=engine,
            redis=redis,
            devices=device_service,
            queries=queries,
            commands_=command_service,
        )
        for _ in range(2)
    ]
    created = []
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    for round_ in range(RACE_ROUNDS):
        automation = await create(
            automation_service,
            household,
            {
                "triggers": [
                    {
                        "type": "telemetry",
                        "device_id": motion,
                        "metric": "motion",
                        "op": "eq",
                        "value": False,
                        "for_s": 60,
                    }
                ],
                "actions": [command(hall, on=False)],
            },
        )
        created.append(automation)
        await workers[0].handle_event(
            f"{round_}-on",
            DeviceEvent(household.home, motion, DeviceEventKind.TELEMETRY, t0, {"motion": True}),
        )
        await workers[0].handle_event(
            f"{round_}-off",
            DeviceEvent(
                household.home,
                motion,
                DeviceEventKind.TELEMETRY,
                t0 + timedelta(seconds=1),
                {"motion": False},
            ),
        )

    await asyncio.gather(*(w.fire_due_timers() for w in workers))

    for automation in created:
        assert [s for s, _ in await runs(engine, automation.id)] == ["completed"]


async def test_two_workers_sweeping_schedules_run_each_slot_once(
    *,
    migrated_database: Settings,
    engine: AsyncEngine,
    redis: Redis,
    household: Household,
    device_service: DevicesService,
    queries: TelemetryQueries,
    command_service: CommandsService,
    automation_service: AutomationsService,
) -> None:
    hall = household.devices["hall"]
    workers = [
        build_engine(
            settings=migrated_database,
            engine=engine,
            redis=redis,
            devices=device_service,
            queries=queries,
            commands_=command_service,
        )
        for _ in range(2)
    ]
    created = [
        await create(
            automation_service,
            household,
            {
                "triggers": [{"type": "schedule", "at": "03:00"}],
                "actions": [command(hall, on=False)],
            },
        )
        for _ in range(RACE_ROUNDS)
    ]
    slot = datetime.now(UTC) - timedelta(seconds=30)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE automations.schedules SET next_at = :slot WHERE automation_id = ANY(:ids)"
            ),
            {"slot": slot, "ids": [a.id for a in created]},
        )

    await asyncio.gather(*(w.fire_due_schedules() for w in workers))

    for automation in created:
        assert await runs(engine, automation.id) == [
            ("completed", f"schedule:0:{slot.isoformat()}")
        ]
    async with engine.connect() as conn:
        due = await conn.scalar(
            text(
                "SELECT count(*) FROM automations.schedules WHERE next_at <= now()"
                " AND automation_id = ANY(:ids)"
            ),
            {"ids": [a.id for a in created]},
        )
    assert due == 0


async def test_two_automations_that_feed_each_other_are_stopped_by_the_loop_guard(
    *,
    engine: AsyncEngine,
    redis: Redis,
    stream: str,
    household: Household,
    device_service: DevicesService,
    automation_service: AutomationsService,
    automation_engine: AutomationEngine,
) -> None:
    """Hall on -> porch on; porch on -> hall off; hall off -> porch off; porch off -> hall
    on... with devices that do what they are told. It has to stop on its own."""
    hall, porch = household.devices["hall"], household.devices["porch"]

    def when(device: str, on: bool) -> dict[str, Any]:
        return {
            "type": "device_state",
            "device_id": device,
            "property": "on",
            "op": "eq",
            "value": on,
        }

    pairs = [
        (hall, True, porch, True),
        (porch, True, hall, False),
        (hall, False, porch, False),
        (porch, False, hall, True),
    ]
    created = []
    for i, (watched, value, target, set_to) in enumerate(pairs):
        saved = await automation_service.create(
            home_id=household.home,
            timezone="UTC",
            name=f"ping {i}",
            description=None,
            enabled=True,
            definition={
                "schema_version": "1",
                "triggers": [when(watched, value)],
                "actions": [command(target, on=set_to)],
            },
            actor=household.owner.principal.user_id,
        )
        created.append(saved)
    assert created[-1].warnings, "saving the last one closes the loop and says so"

    consumer = StreamConsumer(
        redis,
        stream=stream,
        group="automations",
        consumer="w1",
        handler=automation_engine.handle_event,
        block_ms=100,
    )
    await consumer.ensure_group()
    stop = asyncio.Event()
    seen: set[UUID] = set()

    async def devices_obey() -> None:
        """Each new command becomes the device's reported state, as a device would."""
        clock = datetime.now(UTC)
        while not stop.is_set():
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT id, device_id, desired FROM commands.commands"
                            " WHERE home_id = :home ORDER BY issued_at"
                        ),
                        {"home": household.home},
                    )
                ).all()
            for row in rows:
                if row.id not in seen:
                    seen.add(row.id)
                    clock += timedelta(milliseconds=10)
                    await device_service.record_reported_state(
                        home_id=household.home,
                        device_id=row.device_id,
                        reported=row.desired,
                        at=clock,
                    )
            await asyncio.sleep(0.05)

    tasks = [asyncio.create_task(consumer.run(stop)), asyncio.create_task(devices_obey())]
    await device_service.record_reported_state(
        home_id=household.home,
        device_id=hall,
        reported={"on": False},
        at=datetime.now(UTC) - timedelta(seconds=5),
    )
    await device_service.record_reported_state(
        home_id=household.home,
        device_id=porch,
        reported={"on": False},
        at=datetime.now(UTC) - timedelta(seconds=5),
    )
    await device_service.record_reported_state(
        home_id=household.home,
        device_id=hall,
        reported={"on": True},
        at=datetime.now(UTC) - timedelta(seconds=4),
    )

    async def suspended() -> bool:
        statuses = [
            (await automation_service.get(household.home, s.automation.id)).status for s in created
        ]
        return AutomationStatus.SUSPENDED in statuses

    await eventually(suspended, within_s=30)
    await asyncio.sleep(1)  # let anything in flight settle
    issued = len(seen)
    await asyncio.sleep(1)
    stop.set()
    await asyncio.gather(*tasks)

    assert issued == len(seen), "no commands after the suspension"
    assert issued <= 6  # the default chain limit (5), plus the run that tripped it
    async with engine.connect() as conn:
        reasons = (
            (
                await conn.execute(
                    text(
                        "SELECT reason FROM automations.runs WHERE status = 'suppressed'"
                        " AND automation_id = ANY(:ids)"
                    ),
                    {"ids": [s.automation.id for s in created]},
                )
            )
            .scalars()
            .all()
        )
    assert any(r and r.startswith("loop suspected: triggered by a chain") for r in reasons)


async def test_revisions_are_append_only_and_finished_runs_immutable(
    *,
    engine: AsyncEngine,
    household: Household,
    automation_service: AutomationsService,
    automation_engine: AutomationEngine,
) -> None:
    automation = await create(
        automation_service,
        household,
        {
            "triggers": [{"type": "schedule", "at": "03:00"}],
            "actions": [command(household.devices["hall"], on=True)],
        },
    )

    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE automations.revisions SET name = 'x' WHERE automation_id = :id"),
                {"id": automation.id},
            )
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM automations.revisions WHERE automation_id = :id"),
                {"id": automation.id},
            )

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE automations.schedules SET next_at = now() WHERE automation_id = :id"),
            {"id": automation.id},
        )
    assert await automation_engine.fire_due_schedules() >= 1
    with pytest.raises(DBAPIError, match="already finished"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE automations.runs SET status = 'failed' WHERE automation_id = :id"),
                {"id": automation.id},
            )


async def test_a_run_that_was_never_finished_is_settled_by_the_sweeper(
    *,
    engine: AsyncEngine,
    household: Household,
    automation_service: AutomationsService,
    automation_engine: AutomationEngine,
) -> None:
    automation = await create(
        automation_service,
        household,
        {
            "triggers": [{"type": "schedule", "at": "03:00"}],
            "actions": [command(household.devices["hall"], on=True)],
        },
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO automations.runs (id, automation_id, home_id, automation_version,"
                " trigger_index, event_key, status, depth, started_at)"
                " VALUES (gen_random_uuid(), :id, :home, 1, 0, 'crashed', 'running', 0,"
                " now() - interval '10 minutes')"
            ),
            {"id": automation.id, "home": household.home},
        )

    assert await automation_engine.settle_interrupted() >= 1
    assert await runs(engine, automation.id) == [(RunStatus.FAILED.value, "crashed")]
