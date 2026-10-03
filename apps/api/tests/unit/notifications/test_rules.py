"""Alert rules, email timing and webhook signatures: pure functions, no I/O."""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from device_protocol import DeviceKind
from smarthome.modules.notifications.domain.errors import InvalidWebhook
from smarthome.modules.notifications.domain.model import (
    AlertKind,
    Clear,
    Preferences,
    Raise,
    Severity,
    Timing,
)
from smarthome.modules.notifications.domain.rules import (
    DeviceInfo,
    email_at,
    in_quiet_hours,
    on_budget,
    on_device_event,
)
from smarthome.modules.notifications.domain.webhook import signature, validate_url, verify
from smarthome.shared.events import DeviceEvent, DeviceEventKind

HOME = UUID("00000000-0000-0000-0000-00000000000a")
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
T0 = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # Monday 12:00 in São Paulo
TIMING = Timing()
SENSOR = DeviceInfo("dev-1", "Hall sensor", DeviceKind.MOTION_SENSOR)
LOCK = DeviceInfo("dev-2", "Front door", DeviceKind.LOCK)


def event(kind: DeviceEventKind, data: dict[str, object], device: str = "dev-1") -> DeviceEvent:
    return DeviceEvent(HOME, device, kind, T0, data)


def test_a_device_going_offline_raises_an_alert_due_after_the_grace_period() -> None:
    [intent] = on_device_event(event(DeviceEventKind.PRESENCE, {"online": False}), SENSOR, TIMING)
    assert isinstance(intent, Raise)
    assert intent.key == "device_offline:dev-1"
    assert intent.kind is AlertKind.DEVICE_OFFLINE
    assert intent.due_at == T0 + timedelta(minutes=5)
    assert intent.severity is Severity.WARNING
    assert intent.title == "Hall sensor is offline"


def test_a_lock_going_offline_is_critical() -> None:
    [intent] = on_device_event(
        event(DeviceEventKind.PRESENCE, {"online": False}, "dev-2"), LOCK, TIMING
    )
    assert isinstance(intent, Raise)
    assert intent.severity is Severity.CRITICAL


def test_coming_back_online_clears_the_offline_alert() -> None:
    [intent] = on_device_event(event(DeviceEventKind.PRESENCE, {"online": True}), SENSOR, TIMING)
    assert intent == Clear("device_offline:dev-1", T0)


@pytest.mark.parametrize(
    ("pct", "expected"),
    [(9.5, "raise"), (14.9, "raise"), (15.0, None), (19.9, None), (20.0, "clear"), (88, "clear")],
)
def test_low_battery_has_hysteresis_between_15_and_20_percent(
    pct: float, expected: str | None
) -> None:
    intents = on_device_event(
        event(DeviceEventKind.TELEMETRY, {"battery_pct": pct, "rssi_dbm": -60}), SENSOR, TIMING
    )
    kinds = {"raise": Raise, "clear": Clear}
    assert [type(i) for i in intents] == ([kinds[expected]] if expected else [])
    if expected == "raise":
        assert isinstance(intents[0], Raise)
        assert intents[0].due_at is None  # opens at once
        assert intents[0].details == {"battery_pct": pct}


def test_telemetry_without_a_battery_reading_and_other_kinds_change_nothing() -> None:
    assert on_device_event(event(DeviceEventKind.TELEMETRY, {"motion": True}), SENSOR, TIMING) == []
    assert on_device_event(event(DeviceEventKind.STATE, {"on": True}), SENSOR, TIMING) == []
    assert (
        on_device_event(event(DeviceEventKind.TELEMETRY, {"battery_pct": True}), SENSOR, TIMING)
        == []
    )


def test_budget_alerts_open_at_80_and_100_percent_keyed_by_month() -> None:
    month = datetime(2026, 10, 1, tzinfo=SAO_PAULO)

    def raised(used: float) -> list[str]:
        return [
            r.key
            for r in on_budget(
                month_start=month, used_kwh=used, budget_kwh=200, now=T0, timing=TIMING
            )
        ]

    assert raised(150) == []
    assert raised(160) == ["energy_budget:2026-10:80"]
    assert raised(201) == ["energy_budget:2026-10:80", "energy_budget:2026-10:100"]
    [at_100] = [
        r
        for r in on_budget(month_start=month, used_kwh=201, budget_kwh=200, now=T0, timing=TIMING)
        if r.key.endswith(":100")
    ]
    assert at_100.severity is Severity.WARNING
    assert at_100.details == {
        "month": "2026-10",
        "used_kwh": 201,
        "budget_kwh": 200,
        "percent": 100,
    }


@pytest.mark.parametrize(
    ("hour", "start", "end", "quiet"),
    [
        (23, 22, 7, True),
        (3, 22, 7, True),
        (7, 22, 7, False),
        (12, 22, 7, False),
        (13, 12, 14, True),
    ],
)
def test_quiet_hours_may_wrap_past_midnight(hour: int, start: int, end: int, quiet: bool) -> None:
    assert in_quiet_hours(hour, start, end) is quiet


def test_email_waits_for_quiet_hours_to_end_except_for_critical_alerts() -> None:
    prefs = Preferences(quiet_start=22, quiet_end=7)
    late = datetime(2026, 10, 5, 23, 30, tzinfo=SAO_PAULO).astimezone(UTC)

    assert email_at(prefs, Severity.WARNING, now=late, tz=SAO_PAULO) == datetime(
        2026, 10, 6, 7, 0, tzinfo=SAO_PAULO
    )
    assert email_at(prefs, Severity.CRITICAL, now=late, tz=SAO_PAULO) == late
    noon = datetime(2026, 10, 5, 12, 0, tzinfo=SAO_PAULO)
    assert email_at(prefs, Severity.WARNING, now=noon, tz=SAO_PAULO) == noon


def test_email_respects_the_switch_and_the_minimum_severity() -> None:
    assert (
        email_at(Preferences(email_enabled=False), Severity.CRITICAL, now=T0, tz=SAO_PAULO) is None
    )
    assert email_at(Preferences(), Severity.INFO, now=T0, tz=SAO_PAULO) is None
    everything = Preferences(min_email_severity=Severity.INFO)
    assert email_at(everything, Severity.INFO, now=T0, tz=SAO_PAULO) == T0


@pytest.mark.parametrize(("start", "end"), [(22, None), (None, 7), (24, 7), (5, 5)])
def test_preferences_reject_half_or_impossible_quiet_hours(
    start: int | None, end: int | None
) -> None:
    with pytest.raises(ValueError, match="quiet hours"):
        Preferences(quiet_start=start, quiet_end=end)


def test_a_signed_delivery_verifies_and_tampering_or_replay_fails() -> None:
    body = b'{"type":"alert.opened"}'
    header = signature("s3cret" * 6, 1_700_000_000, body)
    assert header.startswith("t=1700000000,v1=")
    assert verify("s3cret" * 6, header, body, now=1_700_000_010)
    assert not verify("s3cret" * 6, header, body + b" ", now=1_700_000_010)
    assert not verify("other" * 7, header, body, now=1_700_000_010)
    assert not verify("s3cret" * 6, header, body, now=1_700_000_000 + 301)  # too old
    assert not verify("s3cret" * 6, "garbage", body, now=1_700_000_000)


@pytest.mark.parametrize(
    ("url", "allow_http", "error"),
    [
        ("https://hooks.example.com/x", False, None),
        ("http://hooks.example.com/x", False, "https"),
        ("http://localhost:9000/x", True, None),
        ("https://user:pw@hooks.example.com/x", False, "credentials"),
        ("https:///x", False, "host"),
        ("https://hooks.example.com/x#frag", False, "fragment"),
        ("ftp://hooks.example.com/x", True, "https"),
        ("https://e.com/" + "a" * 500, False, "500"),
    ],
)
def test_webhook_urls_are_validated(url: str, allow_http: bool, error: str | None) -> None:
    if error is None:
        assert validate_url(url, allow_http=allow_http) == url
    else:
        with pytest.raises(InvalidWebhook, match=error):
            validate_url(url, allow_http=allow_http)


def test_retries_back_off_exponentially_up_to_an_hour() -> None:
    assert [TIMING.retry_after(n).total_seconds() for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert TIMING.retry_after(20) == timedelta(hours=1)
