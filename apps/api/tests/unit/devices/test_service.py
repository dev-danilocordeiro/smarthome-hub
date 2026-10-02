"""DevicesService with in-memory adapters: pairing saga, lifecycle ordering, ingestion guards."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from device_protocol import DeviceKind
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.devices.domain.errors import DeviceNotFound, InvalidPairingCode
from smarthome.modules.devices.domain.model import Device, DeviceStatus, PairingCode, Twin
from smarthome.shared.audit import AuditEvent
from tests.unit.identity.fakes import FakeClock

HOME = uuid4()
OTHER_HOME = uuid4()


@dataclass
class Store:
    devices: dict[str, Device] = field(default_factory=dict)
    codes: dict[bytes, PairingCode] = field(default_factory=dict)
    twins: dict[str, Twin] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)
    fail_next_commit: bool = False


class Repo:
    def __init__(self, s: Store) -> None:
        self.s = s


class Devices(Repo):
    async def add(self, d: Device) -> None:
        self.s.devices[d.id] = d

    async def get(self, i: str) -> Device | None:
        return self.s.devices.get(i)

    lock = get

    async def for_home(self, h: UUID) -> list[Device]:
        return [
            d
            for d in self.s.devices.values()
            if d.home_id == h and d.status is not DeviceStatus.REVOKED
        ]

    async def save(self, d: Device) -> None:
        self.s.devices[d.id] = d


class Codes(Repo):
    async def add(self, c: PairingCode) -> None:
        self.s.codes[c.code_hash] = c

    async def find(self, h: bytes) -> PairingCode | None:
        return self.s.codes.get(h)

    lock = find

    async def mark_claimed(self, code_id: UUID, *, device_id: str, at: datetime) -> None:
        for h, c in self.s.codes.items():
            if c.id == code_id:
                from dataclasses import replace  # noqa: PLC0415

                self.s.codes[h] = replace(c, claimed_at=at, claimed_device_id=device_id)


class Twins(Repo):
    async def add(self, t: Twin) -> None:
        self.s.twins[t.device_id] = t

    async def get(self, i: str) -> Twin | None:
        return self.s.twins.get(i)

    lock = get

    async def save_reported(self, t: Twin) -> None:
        self.s.twins[t.device_id] = t


class Audit(Repo):
    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None:
        self.s.audit.append(event)


class UoW:
    """Applies writes directly; a failing commit is simulated by raising before return."""

    def __init__(self, s: Store) -> None:
        self.s = s
        self.devices, self.pairing_codes = Devices(s), Codes(s)
        self.twins, self.audit = Twins(s), Audit(s)

    async def commit(self) -> None:
        if self.s.fail_next_commit:
            self.s.fail_next_commit = False
            raise ConnectionError("database went away")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None


@dataclass
class FakeBroker:
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    async def register_device(self, *, home_id: str, device_id: str, password: str) -> None:
        self.calls.append(("register", device_id))

    async def disable_device(self, device_id: str) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.calls.append(("disable", device_id))

    async def enable_device(self, device_id: str) -> None:
        self.calls.append(("enable", device_id))

    async def remove_device(self, device_id: str) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.calls.append(("remove", device_id))


@dataclass
class FakeLive:
    presence: dict[str, bool] = field(default_factory=dict)
    reported: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def set_presence(self, device_id: str, *, online: bool, at: datetime) -> None:
        self.presence[device_id] = online

    async def set_reported(self, device_id: str, reported: dict[str, Any], *, at: datetime) -> None:
        self.reported[device_id] = reported

    async def forget(self, device_id: str) -> None:
        self.presence.pop(device_id, None)


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def live() -> FakeLive:
    return FakeLive()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(store: Store, broker: FakeBroker, live: FakeLive, clock: FakeClock) -> DevicesService:
    return DevicesService(lambda: UoW(store), broker, live, clock)


async def paired(service: DevicesService, kind: DeviceKind = DeviceKind.LIGHT) -> Device:
    _, code = await service.create_pairing_code(
        home_id=HOME, actor="alice", name="Lamp", room="office"
    )
    return (await service.claim(code=code, kind=kind, firmware="1.0")).device


async def test_claiming_a_code_creates_an_active_device_with_broker_credentials_and_audits_it(
    service: DevicesService, store: Store, broker: FakeBroker
) -> None:
    _, code = await service.create_pairing_code(home_id=HOME, actor="alice", name="Lamp")

    claimed = await service.claim(code=code, kind=DeviceKind.LIGHT, firmware="1.0")

    assert claimed.device.status is DeviceStatus.ACTIVE
    assert claimed.device.name == "Lamp"
    assert len(claimed.mqtt_password) >= 40
    assert broker.calls == [("register", claimed.device.id)]
    assert [e.action for e in store.audit] == ["pairing_code.created", "device.paired"]


async def test_an_invalid_code_is_rejected_before_any_broker_credentials_exist(
    service: DevicesService, broker: FakeBroker
) -> None:
    with pytest.raises(InvalidPairingCode):
        await service.claim(code="ZZZZ-ZZZZ", kind=DeviceKind.LIGHT, firmware=None)

    assert broker.calls == []


async def test_if_recording_the_claim_fails_the_broker_credentials_are_removed_again(
    service: DevicesService, store: Store, broker: FakeBroker
) -> None:
    _, code = await service.create_pairing_code(home_id=HOME, actor="alice")
    store.fail_next_commit = True

    with pytest.raises(ConnectionError):
        await service.claim(code=code, kind=DeviceKind.PLUG, firmware=None)

    (registered,) = [d for op, d in broker.calls if op == "register"]
    assert ("remove", registered) in broker.calls


async def test_a_second_claim_with_the_same_code_fails(service: DevicesService) -> None:
    _, code = await service.create_pairing_code(home_id=HOME, actor="alice")
    await service.claim(code=code, kind=DeviceKind.PLUG, firmware=None)

    with pytest.raises(InvalidPairingCode):
        await service.claim(code=code, kind=DeviceKind.PLUG, firmware=None)


async def test_an_expired_code_cannot_be_claimed(service: DevicesService, clock: FakeClock) -> None:
    _, code = await service.create_pairing_code(home_id=HOME, actor="alice")
    clock.advance(timedelta(minutes=11))

    with pytest.raises(InvalidPairingCode):
        await service.claim(code=code, kind=DeviceKind.PLUG, firmware=None)


async def test_a_device_of_another_home_is_invisible(service: DevicesService) -> None:
    device = await paired(service)

    with pytest.raises(DeviceNotFound):
        await service.get(OTHER_HOME, device.id)


async def test_if_the_broker_cannot_cut_a_device_off_its_status_is_not_changed(
    service: DevicesService, store: Store, broker: FakeBroker
) -> None:
    device = await paired(service)
    broker.fail = True

    with pytest.raises(ConnectionError):
        await service.revoke(HOME, device.id, actor="alice", reason="lost")

    assert store.devices[device.id].status is DeviceStatus.ACTIVE


async def test_revoking_cuts_broker_access_first_then_records_and_audits(
    service: DevicesService, store: Store, broker: FakeBroker, live: FakeLive
) -> None:
    device = await paired(service)
    live.presence[device.id] = True

    await service.revoke(HOME, device.id, actor="alice", reason="sold it")

    assert broker.calls[-1] == ("remove", device.id)
    assert store.devices[device.id].status is DeviceStatus.REVOKED
    assert store.audit[-1].action == "device.revoked"
    assert device.id not in live.presence
    assert await service.list_devices(HOME) == []


async def test_presence_from_a_topic_naming_the_wrong_home_is_ignored(
    service: DevicesService, live: FakeLive, clock: FakeClock
) -> None:
    device = await paired(service)

    accepted = await service.record_presence(
        home_id=OTHER_HOME, device_id=device.id, online=True, at=clock.now()
    )

    assert not accepted
    assert device.id not in live.presence


async def test_a_quarantined_device_is_not_reported_online(
    service: DevicesService, clock: FakeClock
) -> None:
    device = await paired(service)
    await service.quarantine(HOME, device.id, actor="alice", reason="flooding")

    assert not await service.record_presence(
        home_id=HOME, device_id=device.id, online=True, at=clock.now() + timedelta(seconds=1)
    )


async def test_reported_state_reaches_the_twin_and_the_live_view(
    service: DevicesService, live: FakeLive, clock: FakeClock
) -> None:
    device = await paired(service)

    assert await service.record_reported_state(
        home_id=HOME, device_id=device.id, reported={"on": True}, at=clock.now()
    )

    assert (await service.get(HOME, device.id)).twin.reported == {"on": True}
    assert live.reported[device.id] == {"on": True}


def test_utc_is_used_throughout() -> None:
    assert FakeClock().now().tzinfo is UTC


async def test_a_scoped_guest_lists_only_the_devices_in_their_scope(
    service: DevicesService,
) -> None:
    door = await paired(service, DeviceKind.LOCK)
    await paired(service, DeviceKind.CAMERA)

    visible = await service.list_devices(HOME, scope=frozenset({door.id}))

    assert [d.id for d in visible] == [door.id]
