"""The outbox relay and command bookkeeping against real Postgres, including the races
their locks exist for."""

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from device_protocol import DeviceKind
from smarthome.modules.commands import wiring as commands
from smarthome.modules.commands.application.services import CommandsService
from smarthome.modules.commands.domain.model import AckStatus, CommandAction, CommandStatus
from smarthome.modules.commands.infrastructure.devices import DevicesModuleAdapter
from smarthome.modules.commands.infrastructure.persistence import PostgresCommandsUnitOfWork
from smarthome.modules.devices.application.services import DevicesService
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings
from smarthome.shared.infrastructure.outbox import OutboxRelay, PostgresOutbox
from smarthome.shared.outbox import OutboxMessage

RACE_ROUNDS = 10


class RecordingPublisher:
    def __init__(self, *, delay_s: float = 0.0, fail: bool = False) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []
        self.delay_s = delay_s
        self.fail = fail

    async def publish(
        self, topic: str, payload: bytes, *, qos: int, headers: Mapping[str, str]
    ) -> None:
        await asyncio.sleep(self.delay_s)
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.published.append((topic, payload, dict(headers)))

    def on(self, prefix: str) -> list[str]:
        return [t for t, _, _ in self.published if t.startswith(prefix)]


class ShiftedClock:
    def __init__(self, offset: timedelta) -> None:
        self.offset = offset

    def now(self) -> datetime:
        return SystemClock().now() + self.offset


async def enqueue(
    engine: AsyncEngine, prefix: str, count: int, *, expires_at: datetime | None = None
) -> None:
    now = SystemClock().now()
    async with engine.begin() as conn:
        outbox = PostgresOutbox(conn)
        for i in range(count):
            await outbox.add(
                OutboxMessage(topic=f"{prefix}/{i:04d}", payload={"n": i}, expires_at=expires_at),
                created_at=now,
            )


async def test_committed_messages_are_published_once_in_order(engine: AsyncEngine) -> None:
    prefix = f"test/{uuid4().hex}"
    await enqueue(engine, prefix, 5)
    publisher = RecordingPublisher()
    relay = OutboxRelay(engine, publisher, SystemClock(), batch_size=1000, listen=False)

    await relay.relay_once()
    await relay.relay_once()

    assert publisher.on(prefix) == [f"{prefix}/{i:04d}" for i in range(5)]


async def test_a_rolled_back_transaction_leaves_nothing_to_publish(engine: AsyncEngine) -> None:
    prefix = f"test/{uuid4().hex}"
    async with engine.connect() as conn, conn.begin() as tx:
        await PostgresOutbox(conn).add(
            OutboxMessage(topic=f"{prefix}/x", payload={}), created_at=SystemClock().now()
        )
        await tx.rollback()
    publisher = RecordingPublisher()

    await OutboxRelay(engine, publisher, SystemClock(), listen=False).relay_once()

    assert publisher.on(prefix) == []


async def test_relays_running_side_by_side_never_publish_a_message_twice(
    engine: AsyncEngine,
) -> None:
    prefix = f"test/{uuid4().hex}"
    await enqueue(engine, prefix, 60)
    # Slow publishing keeps rows locked while the other relays look for work.
    publisher = RecordingPublisher(delay_s=0.01)
    relays = [
        OutboxRelay(engine, publisher, SystemClock(), batch_size=10, listen=False) for _ in range(4)
    ]

    for _ in range(6):
        await asyncio.gather(*(r.relay_once() for r in relays))

    published = publisher.on(prefix)
    assert len(published) == 60
    assert len(set(published)) == 60


async def test_an_unreachable_broker_delays_messages_without_losing_or_reordering_them(
    engine: AsyncEngine,
) -> None:
    prefix = f"test/{uuid4().hex}"
    await enqueue(engine, prefix, 3)
    failing = RecordingPublisher(fail=True)

    result = await OutboxRelay(engine, failing, SystemClock(), listen=False).relay_once()

    assert result.failed == 1
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT attempts, last_error, next_attempt_at > now() AS backing_off"
                    " FROM outbox.messages WHERE topic LIKE :p ORDER BY id"
                ),
                {"p": f"{prefix}/%"},
            )
        ).all()
    assert [r.attempts for r in rows] == [1, 0, 0]
    assert rows[0].last_error == "ConnectionError: broker unreachable"
    assert rows[0].backing_off

    healthy = RecordingPublisher()
    later = OutboxRelay(engine, healthy, ShiftedClock(timedelta(seconds=5)), listen=False)
    await later.relay_once()
    assert healthy.on(prefix) == [f"{prefix}/0000", f"{prefix}/0001", f"{prefix}/0002"]


async def test_a_message_past_its_deadline_is_dropped_not_published(engine: AsyncEngine) -> None:
    prefix = f"test/{uuid4().hex}"
    await enqueue(engine, prefix, 1, expires_at=SystemClock().now() - timedelta(seconds=1))
    publisher = RecordingPublisher()

    result = await OutboxRelay(engine, publisher, SystemClock(), listen=False).relay_once()

    assert publisher.on(prefix) == []
    assert result.expired >= 1
    async with engine.connect() as conn:
        outcome = await conn.scalar(
            text("SELECT outcome FROM outbox.messages WHERE topic = :t"), {"t": f"{prefix}/0000"}
        )
    assert outcome == "expired"


async def test_a_commit_wakes_the_relay_without_waiting_for_the_poll(engine: AsyncEngine) -> None:
    prefix = f"test/{uuid4().hex}"
    publisher = RecordingPublisher()
    relay = OutboxRelay(engine, publisher, SystemClock(), poll_interval_s=60)
    stop = asyncio.Event()
    running = asyncio.create_task(relay.run(stop))
    try:
        await asyncio.sleep(0.5)  # first drain done; now it waits on LISTEN or the 60 s poll
        await enqueue(engine, prefix, 1)
        async with asyncio.timeout(3):
            while not publisher.on(prefix):  # noqa: ASYNC110 - another task publishes
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(running, timeout=5)


async def test_processed_messages_are_purged_after_the_retention_window(
    engine: AsyncEngine,
) -> None:
    prefix = f"test/{uuid4().hex}"
    await enqueue(engine, prefix, 2)
    await OutboxRelay(engine, RecordingPublisher(), SystemClock(), listen=False).relay_once()
    later = OutboxRelay(engine, RecordingPublisher(), ShiftedClock(timedelta(days=2)), listen=False)

    assert await later.purge_processed(older_than=timedelta(days=1)) >= 2
    async with engine.connect() as conn:
        left = await conn.scalar(
            text("SELECT count(*) FROM outbox.messages WHERE topic LIKE :p"), {"p": f"{prefix}/%"}
        )
    assert left == 0


# --- Commands -------------------------------------------------------------------------


async def paired(service: DevicesService, kind: DeviceKind) -> tuple[str, UUID]:
    home = uuid4()
    _, code = await service.create_pairing_code(home_id=home, actor="alice")
    claimed = await service.claim(code=code, kind=kind, firmware=None)
    return claimed.device.id, home


@pytest.fixture
def commands_service(
    device_settings: Settings, engine: AsyncEngine, service: DevicesService
) -> CommandsService:
    return commands.build_service(
        device_settings, engine=engine, devices=service, clock=SystemClock()
    )


async def test_issuing_writes_command_outbox_row_and_twin_desired_state(
    commands_service: CommandsService, service: DevicesService, engine: AsyncEngine
) -> None:
    device_id, home = await paired(service, DeviceKind.LIGHT)

    command = await commands_service.issue(
        home_id=home,
        device_id=device_id,
        action=CommandAction.SET_STATE,
        desired={"on": True, "brightness_pct": 25},
        actor="alice",
    )

    async with engine.connect() as conn:
        payload = await conn.scalar(
            text("SELECT payload FROM outbox.messages WHERE payload->>'command_id' = :id"),
            {"id": str(command.id)},
        )
    assert payload == command.message()
    twin = (await service.get(home, device_id)).twin
    assert twin.desired == {"on": True, "brightness_pct": 25}
    assert twin.delta() == {"on": True, "brightness_pct": 25}


async def test_the_schema_refuses_to_rewrite_a_settled_command(
    commands_service: CommandsService, service: DevicesService, engine: AsyncEngine
) -> None:
    device_id, home = await paired(service, DeviceKind.PLUG)
    command = await commands_service.issue(
        home_id=home,
        device_id=device_id,
        action=CommandAction.IDENTIFY,
        desired=None,
        actor="alice",
    )
    await commands_service.record_ack(
        home_id=home,
        device_id=device_id,
        command_id=command.id,
        ack=AckStatus.APPLIED,
        reason=None,
    )

    for statement in (
        "UPDATE commands.commands SET status = 'failed', reason = 'x' WHERE id = :id",
        "UPDATE commands.commands SET issued_by = 'mallory' WHERE id = :id",
        "DELETE FROM commands.commands WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match=r"settled|immutable|kept as history"):
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"id": command.id})


async def test_an_ack_racing_the_timeout_sweeper_settles_the_command_exactly_once(
    device_settings: Settings, engine: AsyncEngine, service: DevicesService
) -> None:
    """Without the row lock (and SKIP LOCKED in the sweeper) both would settle the same
    command; the schema guard would then fail one of them, or two audit entries appear."""
    device_id, home = await paired(service, DeviceKind.LOCK)
    issuer = commands.build_service(
        device_settings, engine=engine, devices=service, clock=SystemClock()
    )
    # A clock far enough ahead that every command is overdue for the sweeper.
    late = CommandsService(
        lambda: PostgresCommandsUnitOfWork(engine),
        DevicesModuleAdapter(service),
        ShiftedClock(timedelta(minutes=10)),
        ack_grace=timedelta(seconds=10),
    )
    for _ in range(RACE_ROUNDS):
        command = await issuer.issue(
            home_id=home,
            device_id=device_id,
            action=CommandAction.SET_STATE,
            desired={"locked": True},
            actor="alice",
        )

        await asyncio.gather(
            late.record_ack(
                home_id=home,
                device_id=device_id,
                command_id=command.id,
                ack=AckStatus.APPLIED,
                reason=None,
            ),
            late.expire_overdue(),
        )

        settled = await issuer.get(home, device_id, command.id)
        assert settled.status in {CommandStatus.ACKNOWLEDGED, CommandStatus.TIMED_OUT}
        async with engine.connect() as conn:
            outcomes = await conn.scalar(
                text(
                    "SELECT count(*) FROM audit.entries WHERE details->>'command_id' = :id"
                    " AND action <> 'command.issued'"
                ),
                {"id": str(command.id)},
            )
        assert outcomes == 1
