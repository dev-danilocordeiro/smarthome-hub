from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from device_protocol import DeviceKind
from smarthome.modules.devices.domain.errors import (
    InvalidPairingCode,
    InvalidTransition,
    UnsupportedState,
)
from smarthome.modules.devices.domain.model import (
    CODE_ALPHABET,
    Device,
    DeviceStatus,
    PairingCode,
    Twin,
    hash_code,
    new_device_id,
    normalize_code,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
HOME = uuid4()


def device(**kw: object) -> Device:
    base: dict[str, object] = {
        "id": "light-0123456789ab",
        "home_id": HOME,
        "kind": DeviceKind.LIGHT,
        "name": "Lamp",
        "room": "living_room",
        "firmware": None,
        "status": DeviceStatus.ACTIVE,
        "status_changed_at": NOW,
        "paired_by": "alice",
        "paired_at": NOW,
    }
    return Device(**(base | kw))  # type: ignore[arg-type]


def test_pairing_codes_are_short_unambiguous_and_stored_only_as_a_hash() -> None:
    code, plaintext = PairingCode.issue(home_id=HOME, created_by="alice", now=NOW)

    assert len(plaintext) == 9
    assert plaintext[4] == "-"
    assert set(plaintext.replace("-", "")) <= set(CODE_ALPHABET)
    assert code.code_hash == hash_code(plaintext)
    assert plaintext.encode() not in code.code_hash


@given(st.text(alphabet=CODE_ALPHABET, min_size=8, max_size=8))
def test_a_code_typed_with_dashes_spaces_lowercase_or_lookalike_letters_still_matches(
    raw: str,
) -> None:
    sloppy = f" {raw[:4].lower()} - {raw[4:]} ".replace("0", "O").replace("1", "l")

    assert hash_code(sloppy) == hash_code(raw)
    assert normalize_code(sloppy) == raw


def test_a_code_works_once_and_only_until_it_expires() -> None:
    code, _ = PairingCode.issue(home_id=HOME, created_by="alice", now=NOW)

    code.claim(kind=DeviceKind.PLUG, now=NOW + timedelta(minutes=9))
    with pytest.raises(InvalidPairingCode):
        code.claim(kind=DeviceKind.PLUG, now=code.expires_at)
    used = replace(code, claimed_at=NOW, claimed_device_id="plug-1")
    with pytest.raises(InvalidPairingCode):
        used.claim(kind=DeviceKind.PLUG, now=NOW)


def test_a_code_issued_for_a_lock_cannot_pair_a_camera() -> None:
    code, _ = PairingCode.issue(home_id=HOME, created_by="alice", now=NOW, kind=DeviceKind.LOCK)

    with pytest.raises(InvalidPairingCode, match="different kind"):
        code.claim(kind=DeviceKind.CAMERA, now=NOW)


@pytest.mark.parametrize("kind", list(DeviceKind))
def test_generated_device_ids_are_valid_topic_segments(kind: DeviceKind) -> None:
    from device_protocol import MessageKind, topic  # noqa: PLC0415

    topic(str(HOME), new_device_id(kind), MessageKind.STATE)


def test_quarantine_goes_offline_and_release_restores_the_device() -> None:
    quarantined = device(online=True).quarantine(reason="flooding", now=NOW)

    assert quarantined.status is DeviceStatus.QUARANTINED
    assert not quarantined.online
    assert not quarantined.accepts_traffic
    assert quarantined.release(now=NOW).status is DeviceStatus.ACTIVE


@pytest.mark.parametrize(
    ("start", "action"),
    [
        (DeviceStatus.QUARANTINED, "quarantine"),
        (DeviceStatus.ACTIVE, "release"),
        (DeviceStatus.REVOKED, "release"),
        (DeviceStatus.REVOKED, "revoke"),
    ],
)
def test_impossible_status_changes_are_refused(start: DeviceStatus, action: str) -> None:
    subject = device(status=start)
    change = (
        (lambda: subject.release(now=NOW))
        if action == "release"
        else (lambda: getattr(subject, action)(reason="x", now=NOW))
    )
    with pytest.raises(InvalidTransition):
        change()


def test_a_late_offline_will_does_not_override_a_newer_online() -> None:
    online = device().with_presence(online=True, at=NOW)
    assert online is not None

    assert online.with_presence(online=False, at=NOW - timedelta(seconds=5)) is None
    later = online.with_presence(online=False, at=NOW + timedelta(seconds=5))
    assert later is not None
    assert not later.online


def test_reported_state_is_applied_once_and_never_rolled_back() -> None:
    twin = Twin(device_id="d", kind=DeviceKind.LIGHT, desired={"on": True, "brightness_pct": 40})
    first = twin.with_reported({"on": True, "brightness_pct": 80}, at=NOW)
    assert first is not None

    assert first.with_reported({"on": False}, at=NOW) is None  # duplicate
    assert first.with_reported({"on": False}, at=NOW - timedelta(seconds=1)) is None  # stale
    assert first.delta() == {"brightness_pct": 40}
    assert not first.in_sync


def test_a_device_cannot_report_properties_its_kind_does_not_have() -> None:
    twin = Twin(device_id="d", kind=DeviceKind.PLUG)

    with pytest.raises(UnsupportedState):
        twin.with_reported({"locked": True}, at=NOW)


def test_desired_state_merges_newer_commands_and_ignores_older_ones() -> None:
    twin = Twin(device_id="d", kind=DeviceKind.LIGHT)
    first = twin.with_desired({"on": True, "brightness_pct": 40}, at=NOW)
    assert first is not None
    second = first.with_desired({"brightness_pct": 80}, at=NOW + timedelta(seconds=1))
    assert second is not None

    assert second.desired == {"on": True, "brightness_pct": 80}
    assert second.with_desired({"on": False}, at=NOW) is None  # issued before the last write
    with pytest.raises(UnsupportedState):
        second.with_desired({"locked": True}, at=NOW + timedelta(seconds=2))
