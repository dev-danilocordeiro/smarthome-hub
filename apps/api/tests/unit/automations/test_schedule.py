"""Schedules and time windows in the home's time zone, across daylight saving changes."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from smarthome.modules.automations.domain.definition import ScheduleTrigger, TimeCondition
from smarthome.modules.automations.domain.schedule import (
    next_occurrence,
    occurrences,
    time_condition_holds,
)

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
NEW_YORK = ZoneInfo("America/New_York")
WEEKDAYS = frozenset(range(5))


def test_the_next_slot_is_later_today_or_tomorrow() -> None:
    trigger = ScheduleTrigger(time(22, 30))

    before = datetime(2026, 10, 5, 20, 0, tzinfo=SAO_PAULO)
    after = datetime(2026, 10, 5, 23, 0, tzinfo=SAO_PAULO)

    assert next_occurrence(trigger, SAO_PAULO, after=before) == datetime(
        2026, 10, 5, 22, 30, tzinfo=SAO_PAULO
    )
    assert next_occurrence(trigger, SAO_PAULO, after=after) == datetime(
        2026, 10, 6, 22, 30, tzinfo=SAO_PAULO
    )


def test_weekdays_skip_the_weekend() -> None:
    trigger = ScheduleTrigger(time(7, 0), WEEKDAYS)
    friday_evening = datetime(2026, 10, 9, 20, 0, tzinfo=SAO_PAULO)

    assert next_occurrence(trigger, SAO_PAULO, after=friday_evening) == datetime(
        2026, 10, 12, 7, 0, tzinfo=SAO_PAULO
    )


def test_a_time_skipped_by_spring_forward_fires_an_hour_later_on_the_wall_clock() -> None:
    # 2026-03-08: New York clocks jump from 02:00 to 03:00.
    trigger = ScheduleTrigger(time(2, 30))

    fired = next_occurrence(trigger, NEW_YORK, after=datetime(2026, 3, 8, 0, 0, tzinfo=NEW_YORK))

    assert fired == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert fired.astimezone(NEW_YORK).time() == time(3, 30)


def test_a_time_repeated_by_fall_back_fires_once() -> None:
    # 2026-11-01: New York clocks go from 02:00 back to 01:00; 01:30 happens twice.
    trigger = ScheduleTrigger(time(1, 30))
    start = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

    fired = occurrences(trigger, NEW_YORK, start=start, end=start + timedelta(days=2))

    assert fired[:2] == [
        datetime(2026, 11, 1, 5, 30, tzinfo=UTC),  # first 01:30 (EDT)
        datetime(2026, 11, 2, 6, 30, tzinfo=UTC),  # next day, EST
    ]


@given(
    hour=st.integers(0, 23),
    minute=st.integers(0, 59),
    weekdays=st.frozensets(st.integers(0, 6), min_size=1),
    after=st.datetimes(
        min_value=datetime(2025, 1, 1), max_value=datetime(2030, 12, 31), timezones=st.just(UTC)
    ),
    zone=st.sampled_from([SAO_PAULO, NEW_YORK, ZoneInfo("Europe/Berlin"), ZoneInfo("UTC")]),
)
def test_the_next_slot_is_the_first_matching_wall_clock_time_after_now(
    hour: int, minute: int, weekdays: frozenset[int], after: datetime, zone: ZoneInfo
) -> None:
    trigger = ScheduleTrigger(time(hour, minute), weekdays)

    fired = next_occurrence(trigger, zone, after=after)

    assert after < fired <= after + timedelta(days=8)
    local = fired.astimezone(zone)
    assert local.weekday() in weekdays
    # Equal to `at`, unless `at` did not exist that day (then up to an hour later).
    shift = datetime.combine(local.date(), local.time()) - datetime.combine(
        local.date(), trigger.at
    )
    assert timedelta(0) <= shift <= timedelta(hours=1)


def test_a_window_that_crosses_midnight_holds_late_and_early() -> None:
    night = TimeCondition(after=time(22), before=time(6))

    def at(hour: int) -> datetime:
        return datetime(2026, 10, 5, hour, tzinfo=SAO_PAULO)

    assert time_condition_holds(night, SAO_PAULO, at=at(23))
    assert time_condition_holds(night, SAO_PAULO, at=at(5))
    assert not time_condition_holds(night, SAO_PAULO, at=at(6))
    assert not time_condition_holds(night, SAO_PAULO, at=at(12))


def test_open_ended_windows_and_weekdays() -> None:
    evenings = TimeCondition(after=time(18))
    mornings = TimeCondition(before=time(9), weekdays=WEEKDAYS)
    monday_8 = datetime(2026, 10, 5, 8, tzinfo=SAO_PAULO)
    saturday_8 = datetime(2026, 10, 10, 8, tzinfo=SAO_PAULO)

    assert not time_condition_holds(evenings, SAO_PAULO, at=monday_8)
    assert time_condition_holds(mornings, SAO_PAULO, at=monday_8)
    assert not time_condition_holds(mornings, SAO_PAULO, at=saturday_8)


def test_windows_are_read_in_the_homes_zone_not_utc() -> None:
    evenings = TimeCondition(after=time(18))
    utc_22 = datetime(2026, 10, 5, 22, tzinfo=UTC)  # 19:00 in Sao Paulo, 18:00 in New York

    assert time_condition_holds(evenings, SAO_PAULO, at=utc_22)
    assert time_condition_holds(evenings, NEW_YORK, at=utc_22)
    assert not time_condition_holds(evenings, ZoneInfo("Asia/Tokyo"), at=utc_22)
