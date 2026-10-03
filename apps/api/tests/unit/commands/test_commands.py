"""Commands: what may be sent, how outcomes are tracked, and what goes to the outbox."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from device_protocol import DeviceKind, MessageKind, topic, validate
from smarthome.modules.commands.application.ports import Target
from smarthome.modules.commands.application.services import CommandsService
from smarthome.modules.commands.domain.errors import (
    CommandNotFound,
    InvalidCommand,
    TargetNotFound,
    TargetUnavailable,
)
from smarthome.modules.commands.domain.model import (
    AckStatus,
    Capability,
    Command,
    CommandAction,
    CommandStatus,
    requirements,
)
from smarthome.shared.audit import AuditEvent
from smarthome.shared.events import DeviceEvent, DeviceEventKind
from smarthome.shared.outbox import OutboxMessage

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
HOME = uuid4()


def light_command(**overrides: Any) -> Command:
    arguments: dict[str, Any] = {
        "home_id": HOME,
        "device_id": "light-1",
        "device_kind": DeviceKind.LIGHT,
        "action": CommandAction.SET_STATE,
        "desired": {"on": True, "brightness_pct": 40},
        "issued_by": "alice",
        "now": NOW,
    } | overrides
    return Command.issue(**arguments)


# --- Domain ---------------------------------------------------------------------------


def test_a_command_becomes_a_valid_protocol_message() -> None:
    command = light_command()

    message = command.message()

    validate(MessageKind.COMMAND, message)
    assert message["command_id"] == str(command.id)
    assert message["expires_at"] == "2026-10-02T12:00:30Z"
    assert command.status is CommandStatus.PENDING


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"desired": None}, "needs at least one"),
        ({"desired": {}}, "needs at least one"),
        ({"desired": {"locked": True}}, "has no properties"),
        ({"desired": {"brightness_pct": 140}}, "brightness_pct"),
        ({"action": CommandAction.REBOOT}, "takes no desired state"),
        ({"ttl": timedelta(seconds=1)}, "ttl"),
        ({"ttl": timedelta(hours=1)}, "ttl"),
    ],
)
def test_commands_the_device_could_not_execute_are_refused_up_front(
    overrides: dict[str, Any], error: str
) -> None:
    with pytest.raises(InvalidCommand, match=error):
        light_command(**overrides)


def test_locks_and_cameras_need_security_rights_and_a_recent_sign_in() -> None:
    lock = requirements(DeviceKind.LOCK, CommandAction.SET_STATE)
    light = requirements(DeviceKind.LIGHT, CommandAction.SET_STATE)
    reboot = requirements(DeviceKind.LIGHT, CommandAction.REBOOT)

    assert lock.capabilities == {Capability.CONTROL, Capability.OPERATE_SECURITY}
    assert lock.recent_authentication == timedelta(minutes=5)
    assert light.capabilities == {Capability.CONTROL}
    assert light.recent_authentication is None
    assert reboot.capabilities == {Capability.CONTROL, Capability.MAINTAIN}


def test_acks_move_a_command_forward_and_duplicates_change_nothing() -> None:
    command = light_command()

    delivered = command.with_ack(AckStatus.RECEIVED, at=NOW + timedelta(seconds=1), reason=None)
    assert delivered is not None
    assert delivered.status is CommandStatus.DELIVERED
    assert delivered.with_ack(AckStatus.RECEIVED, at=NOW, reason=None) is None

    applied = delivered.with_ack(AckStatus.APPLIED, at=NOW + timedelta(seconds=2), reason=None)
    assert applied is not None
    assert applied.status is CommandStatus.ACKNOWLEDGED
    assert applied.delivered_at == NOW + timedelta(seconds=1)
    assert applied.completed_at == NOW + timedelta(seconds=2)


def test_an_applied_ack_overtaking_the_received_one_still_records_delivery() -> None:
    applied = light_command().with_ack(AckStatus.APPLIED, at=NOW, reason=None)

    assert applied is not None
    assert applied.status is CommandStatus.ACKNOWLEDGED
    assert applied.delivered_at == NOW
    assert applied.with_ack(AckStatus.RECEIVED, at=NOW, reason=None) is None


@pytest.mark.parametrize(
    ("ack", "status", "reason"),
    [
        (AckStatus.REJECTED, CommandStatus.FAILED, "rejected: superseded"),
        (AckStatus.FAILED, CommandStatus.FAILED, "failed: superseded"),
        (AckStatus.EXPIRED, CommandStatus.TIMED_OUT, "superseded"),
    ],
)
def test_negative_acks_settle_the_command_with_the_devices_reason(
    ack: AckStatus, status: CommandStatus, reason: str
) -> None:
    settled = light_command().with_ack(ack, at=NOW, reason="superseded")

    assert settled is not None
    assert settled.status is status
    assert settled.reason == reason


@given(
    st.lists(st.sampled_from(list(AckStatus)), min_size=1, max_size=8),
    st.integers(min_value=0, max_value=60),
)
def test_once_settled_no_ack_and_no_timeout_changes_the_outcome(
    acks: list[AckStatus], seconds: int
) -> None:
    command = light_command()
    for ack in acks:
        command = command.with_ack(ack, at=NOW, reason=None) or command
        if command.settled:
            break
    if not command.settled:
        return
    settled = command

    for ack in AckStatus:
        assert settled.with_ack(ack, at=NOW + timedelta(seconds=seconds), reason=None) is None
    assert settled.time_out(now=NOW + timedelta(hours=1), grace=timedelta()) is None


def test_a_command_times_out_only_after_its_deadline_plus_the_ack_grace() -> None:
    command = light_command(ttl=timedelta(seconds=30))
    grace = timedelta(seconds=10)

    assert command.time_out(now=NOW + timedelta(seconds=39), grace=grace) is None
    timed_out = command.time_out(now=NOW + timedelta(seconds=40), grace=grace)

    assert timed_out is not None
    assert timed_out.status is CommandStatus.TIMED_OUT


# --- Service with in-memory adapters --------------------------------------------------


@dataclass
class Store:
    commands: dict[UUID, Command] = field(default_factory=dict)
    outbox: list[OutboxMessage] = field(default_factory=list)
    audit: list[AuditEvent] = field(default_factory=list)
    fail_commit: bool = False


class Commands:
    def __init__(self, store: Store, staged: dict[UUID, Command]) -> None:
        self._store, self._staged = store, staged

    async def add(self, command: Command) -> str | None:
        self._staged[command.id] = command
        return "0" * 31 + "1"

    async def get(self, command_id: UUID) -> Command | None:
        return self._store.commands.get(command_id)

    lock = get

    async def recent_for_device(self, device_id: str, *, limit: int) -> list[Command]:
        mine = [c for c in self._store.commands.values() if c.device_id == device_id]
        return sorted(mine, key=lambda c: c.issued_at, reverse=True)[:limit]

    async def lock_overdue(self, before: datetime, *, limit: int) -> list[Command]:
        return [
            c for c in self._store.commands.values() if not c.settled and c.expires_at < before
        ][:limit]

    async def save(self, command: Command) -> None:
        self._staged[command.id] = command


class Outbox:
    def __init__(self, staged: list[OutboxMessage]) -> None:
        self._staged = staged

    async def add(self, message: OutboxMessage, *, created_at: datetime) -> None:
        self._staged.append(message)


class Audit:
    def __init__(self, staged: list[AuditEvent]) -> None:
        self._staged = staged

    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None:
        self._staged.append(event)


class UoW:
    """Stages writes and applies them on commit, so a failed commit leaves no trace."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._commands: dict[UUID, Command] = {}
        self._outbox: list[OutboxMessage] = []
        self._audit: list[AuditEvent] = []
        self.commands = Commands(store, self._commands)
        self.outbox = Outbox(self._outbox)
        self.audit = Audit(self._audit)

    async def commit(self) -> None:
        if self._store.fail_commit:
            raise ConnectionError("database went away")
        self._store.commands.update(self._commands)
        self._store.outbox.extend(self._outbox)
        self._store.audit.extend(self._audit)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@dataclass
class FakeDevices:
    targets: dict[str, Target] = field(default_factory=dict)
    desired: list[tuple[str, dict[str, Any], datetime]] = field(default_factory=list)
    fail_desired: bool = False

    async def target(self, home_id: UUID, device_id: str) -> Target | None:
        target = self.targets.get(device_id)
        return target if target and target.home_id == home_id else None

    async def set_desired(
        self, home_id: UUID, device_id: str, desired: dict[str, Any], *, at: datetime
    ) -> None:
        if self.fail_desired:
            raise ConnectionError("devices database went away")
        self.desired.append((device_id, desired, at))


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


class Events:
    def __init__(self) -> None:
        self.published: list[DeviceEvent] = []

    async def publish(self, event: DeviceEvent) -> None:
        self.published.append(event)


@dataclass
class World:
    store: Store
    devices: FakeDevices
    clock: Clock
    service: CommandsService
    events: Events


@pytest.fixture
def world() -> World:
    store, clock = Store(), Clock()
    devices = FakeDevices(
        {
            "light-1": Target("light-1", HOME, DeviceKind.LIGHT, accepts_commands=True),
            "lock-1": Target("lock-1", HOME, DeviceKind.LOCK, accepts_commands=True),
            "plug-q": Target("plug-q", HOME, DeviceKind.PLUG, accepts_commands=False),
        }
    )
    events = Events()
    service = CommandsService(
        lambda: UoW(store), devices, clock, ack_grace=timedelta(seconds=10), events=events
    )
    return World(store, devices, clock, service, events)


async def issue(world: World, device_id: str = "light-1", **kw: Any) -> Command:
    arguments: dict[str, Any] = {
        "home_id": HOME,
        "device_id": device_id,
        "action": CommandAction.SET_STATE,
        "desired": {"on": True},
        "actor": "alice",
    } | kw
    return await world.service.issue(**arguments)


async def test_issuing_records_the_command_and_its_message_together(world: World) -> None:
    command = await issue(world)

    assert world.store.commands[command.id].status is CommandStatus.PENDING
    [message] = world.store.outbox
    assert message.topic == topic(str(HOME), "light-1", MessageKind.COMMAND)
    assert message.qos == 1
    assert message.payload == command.message()
    assert message.expires_at == command.expires_at
    assert command.trace_id is not None
    assert world.devices.desired == [("light-1", {"on": True}, NOW)]
    assert world.store.audit == []  # everyday devices are not audited per command


async def test_a_failed_commit_queues_nothing_and_leaves_the_twin_alone(world: World) -> None:
    world.store.fail_commit = True

    with pytest.raises(ConnectionError):
        await issue(world)

    assert world.store.commands == {}
    assert world.store.outbox == []
    assert world.devices.desired == []


async def test_a_twin_that_cannot_be_updated_does_not_stop_the_command(world: World) -> None:
    world.devices.fail_desired = True

    command = await issue(world)

    assert world.store.outbox[0].payload["command_id"] == str(command.id)


async def test_commands_reach_only_active_devices_of_the_same_home(world: World) -> None:
    with pytest.raises(TargetUnavailable):
        await issue(world, "plug-q")
    with pytest.raises(TargetNotFound):
        await issue(world, "light-1", home_id=uuid4())
    with pytest.raises(TargetNotFound):
        await issue(world, "ghost")
    assert world.store.outbox == []


async def test_critical_commands_are_audited_when_issued_and_when_settled(world: World) -> None:
    command = await issue(world, "lock-1", desired={"locked": False})

    await world.service.record_ack(
        home_id=HOME, device_id="lock-1", command_id=command.id, ack=AckStatus.APPLIED, reason=None
    )

    assert [e.action for e in world.store.audit] == ["command.issued", "command.acknowledged"]
    assert world.store.audit[0].actor == "alice"
    assert world.store.audit[0].details["desired"] == {"locked": False}


async def test_an_ack_from_another_device_is_ignored(world: World) -> None:
    command = await issue(world)

    applied_elsewhere = await world.service.record_ack(
        home_id=HOME, device_id="lock-1", command_id=command.id, ack=AckStatus.APPLIED, reason=None
    )
    unknown = await world.service.record_ack(
        home_id=HOME, device_id="light-1", command_id=uuid4(), ack=AckStatus.APPLIED, reason=None
    )

    assert not applied_elsewhere
    assert not unknown
    assert world.store.commands[command.id].status is CommandStatus.PENDING


async def test_the_sweeper_times_out_only_commands_past_deadline_and_grace(world: World) -> None:
    old = await issue(world, ttl=timedelta(seconds=5))
    world.clock.at = NOW + timedelta(seconds=20)
    fresh = await issue(world, ttl=timedelta(seconds=30))

    assert await world.service.expire_overdue() == 1
    assert world.store.commands[old.id].status is CommandStatus.TIMED_OUT
    assert world.store.commands[fresh.id].status is CommandStatus.PENDING
    assert await world.service.expire_overdue() == 0


async def test_outcomes_are_announced_to_live_clients_and_duplicates_are_not(
    world: World,
) -> None:
    acked = await issue(world)
    silent = await issue(world, ttl=timedelta(seconds=5))

    for ack in (AckStatus.RECEIVED, AckStatus.APPLIED, AckStatus.APPLIED):
        await world.service.record_ack(
            home_id=HOME, device_id="light-1", command_id=acked.id, ack=ack, reason=None
        )
    world.clock.at = NOW + timedelta(minutes=1)
    await world.service.expire_overdue()

    assert [(e.kind, e.data["command_id"], e.data["status"]) for e in world.events.published] == [
        (DeviceEventKind.COMMAND, str(acked.id), "delivered"),
        (DeviceEventKind.COMMAND, str(acked.id), "acknowledged"),
        (DeviceEventKind.COMMAND, str(silent.id), "timed_out"),
    ]


async def test_a_late_ack_does_not_revive_a_timed_out_command(world: World) -> None:
    command = await issue(world, ttl=timedelta(seconds=5))
    world.clock.at = NOW + timedelta(minutes=1)
    await world.service.expire_overdue()

    changed = await world.service.record_ack(
        home_id=HOME, device_id="light-1", command_id=command.id, ack=AckStatus.APPLIED, reason=None
    )

    assert not changed
    assert world.store.commands[command.id].status is CommandStatus.TIMED_OUT


async def test_a_command_is_only_visible_through_its_own_home_and_device(world: World) -> None:
    command = await issue(world)

    assert (await world.service.get(HOME, "light-1", command.id)).id == command.id
    with pytest.raises(CommandNotFound):
        await world.service.get(HOME, "lock-1", command.id)
    with pytest.raises(CommandNotFound):
        await world.service.get(uuid4(), "light-1", command.id)
