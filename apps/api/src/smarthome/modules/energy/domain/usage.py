"""Turning hourly consumption per device into what a household asks: how much, when, on
what, and what it cost.

Whole-home meters and submeters (smart plugs) measure overlapping things: the meter
already includes every plug. The home total is therefore the meter when there is one,
and the sum of the plugs only when there is not; the difference between meter and plugs
is reported as `unmetered` (everything without a plug of its own).
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

from smarthome.modules.energy.domain.errors import InvalidRange
from smarthome.modules.energy.domain.tariff import Tariff, round_money

MAX_RANGE = timedelta(days=400)
HOUR = timedelta(hours=1)


class Bucket(StrEnum):
    HOUR = "hour"
    DAY = "day"


class MeasuredBy(StrEnum):
    METER = "meter"
    SUBMETERS = "submeters"


@dataclass(frozen=True, slots=True)
class HourlyUsage:
    device_id: str
    hour: datetime  # UTC, start of the hour
    wh: float


@dataclass(frozen=True, slots=True)
class BucketUsage:
    start: datetime  # home time
    wh: float
    cost: Decimal | None


@dataclass(frozen=True, slots=True)
class DeviceUsage:
    device_id: str
    wh: float
    cost: Decimal | None
    whole_home: bool


@dataclass(frozen=True, slots=True)
class UsageReport:
    start: datetime
    end: datetime
    bucket: Bucket
    currency: str | None
    measured_by: MeasuredBy
    total_wh: float
    total_cost: Decimal | None
    unmetered_wh: float | None
    buckets: tuple[BucketUsage, ...]
    devices: tuple[DeviceUsage, ...]


def floor_hour(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def validate_range(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise InvalidRange("`from` and `to` need a UTC offset")
    if start >= end:
        raise InvalidRange("`from` must be before `to`")
    if end - start > MAX_RANGE:
        raise InvalidRange(f"at most {MAX_RANGE.days} days")


def _hours(start: datetime, end: datetime) -> list[datetime]:
    hours, at = [], floor_hour(start)
    while at < end:
        hours.append(at)
        at += HOUR
    return hours


def _cost(wh: float, price: Decimal) -> Decimal:
    # Watt-hours are measured to a milliwatt-hour; anything finer is float noise.
    return Decimal(str(round(wh, 3))) / 1000 * price


def usage_report(
    rows: list[HourlyUsage],
    *,
    whole_home: frozenset[str],
    start: datetime,
    end: datetime,
    bucket: Bucket,
    tz: ZoneInfo,
    tariff: Tariff | None,
) -> UsageReport:
    validate_range(start, end)
    hours = _hours(start, end)
    in_range = set(hours)
    by_hour: dict[datetime, dict[str, float]] = defaultdict(dict)
    for row in rows:
        if row.hour in in_range:
            by_hour[row.hour][row.device_id] = by_hour[row.hour].get(row.device_id, 0.0) + row.wh
    present = {device for usage in by_hour.values() for device in usage}
    meters = present & whole_home
    measured_by = MeasuredBy.METER if meters else MeasuredBy.SUBMETERS

    def bucket_start(hour: datetime) -> datetime:
        local = hour.astimezone(tz)
        if bucket is Bucket.HOUR:
            return local
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    bucket_wh: dict[datetime, float] = {}
    bucket_cost: dict[datetime, Decimal] = {}
    device_wh: dict[str, float] = defaultdict(float)
    device_cost: dict[str, Decimal] = defaultdict(Decimal)
    submeters_wh = 0.0
    for hour in hours:
        key = bucket_start(hour)
        usage = by_hour.get(hour, {})
        home_wh = sum(wh for d, wh in usage.items() if (d in meters) == bool(meters))
        price = tariff.price_at(hour.astimezone(tz)) if tariff else None
        bucket_wh[key] = bucket_wh.get(key, 0.0) + home_wh
        if price is not None:
            bucket_cost[key] = bucket_cost.get(key, Decimal(0)) + _cost(home_wh, price)
        for device_id, wh in usage.items():
            device_wh[device_id] += wh
            if device_id not in whole_home:
                submeters_wh += wh
            if price is not None:
                device_cost[device_id] += _cost(wh, price)

    currency = tariff.currency if tariff else None

    def money(amount: Decimal | None) -> Decimal | None:
        return round_money(amount, currency) if amount is not None and currency else None

    total_wh = sum(bucket_wh.values())
    return UsageReport(
        start=start,
        end=end,
        bucket=bucket,
        currency=currency,
        measured_by=measured_by,
        total_wh=round(total_wh, 3),
        total_cost=money(sum(bucket_cost.values(), Decimal(0)) if tariff else None),
        unmetered_wh=round(max(0.0, total_wh - submeters_wh), 3) if meters else None,
        buckets=tuple(
            BucketUsage(key, round(wh, 3), money(bucket_cost.get(key, Decimal(0))))
            for key, wh in bucket_wh.items()
        ),
        devices=tuple(
            sorted(
                (
                    DeviceUsage(
                        device_id,
                        round(wh, 3),
                        money(device_cost[device_id]),
                        device_id in whole_home,
                    )
                    for device_id, wh in device_wh.items()
                ),
                key=lambda d: (-d.wh, d.device_id),
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    home_id: UUID
    month_start: datetime  # home time
    used_kwh: Decimal
    budget_kwh: Decimal

    @property
    def ratio(self) -> Decimal:
        return self.used_kwh / self.budget_kwh


def month_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Start of this calendar month in home time, and `now`."""
    local = now.astimezone(tz)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now


def home_total_wh(rows: list[HourlyUsage], *, whole_home: frozenset[str]) -> float:
    meters = {r.device_id for r in rows} & whole_home
    return sum(r.wh for r in rows if (r.device_id in meters) == bool(meters))
