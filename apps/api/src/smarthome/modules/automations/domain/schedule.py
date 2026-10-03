"""Wall-clock time in the home's time zone: schedule triggers and time conditions.

Daylight saving time, made explicit:
- a time that does not exist that day (clocks jump forward over it) fires at the same
  offset as before the jump, i.e. one hour later on the wall clock;
- a time that happens twice (clocks fall back) fires once, at the first occurrence.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from smarthome.modules.automations.domain.definition import ScheduleTrigger, TimeCondition


def local_instant(day: date, at: time, zone: ZoneInfo) -> datetime:
    """The UTC instant of `at` on `day` in `zone` (fold=0: first of two, or shifted)."""
    return datetime.combine(day, at).replace(tzinfo=zone, fold=0).astimezone(UTC)


def next_occurrence(trigger: ScheduleTrigger, zone: ZoneInfo, *, after: datetime) -> datetime:
    """The first instant strictly after `after` at which the trigger fires."""
    start = after.astimezone(zone).date() - timedelta(days=1)  # an earlier offset may apply
    for offset in range(9):
        day = start + timedelta(days=offset)
        if day.weekday() not in trigger.weekdays:
            continue
        candidate = local_instant(day, trigger.at, zone)
        if candidate > after:
            return candidate
    raise AssertionError("a schedule with at least one weekday fires within a week")


def occurrences(
    trigger: ScheduleTrigger, zone: ZoneInfo, *, start: datetime, end: datetime
) -> list[datetime]:
    """Every firing in (start, end]."""
    found: list[datetime] = []
    current = next_occurrence(trigger, zone, after=start)
    while current <= end:
        found.append(current)
        current = next_occurrence(trigger, zone, after=current)
    return found


def time_condition_holds(condition: TimeCondition, zone: ZoneInfo, *, at: datetime) -> bool:
    local = at.astimezone(zone)
    if local.weekday() not in condition.weekdays:
        return False
    now = local.time().replace(tzinfo=None)
    after, before = condition.after, condition.before
    if after is None and before is None:
        return True
    if after is None:
        return now < before  # type: ignore[operator]
    if before is None:
        return now >= after
    if after < before:
        return after <= now < before
    return now >= after or now < before  # wraps midnight
