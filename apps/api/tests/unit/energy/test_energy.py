"""Energy accounting: tariffs, the usage report, and the hourly rollup."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from smarthome.modules.energy.application.ports import Increase, MeteredDevice
from smarthome.modules.energy.application.services import EnergyRollup, EnergyService
from smarthome.modules.energy.domain.errors import (
    InvalidRange,
    InvalidTariff,
    VersionMismatch,
    VersionRequired,
)
from smarthome.modules.energy.domain.tariff import Period, Tariff, parse_price, round_money
from smarthome.modules.energy.domain.usage import (
    Bucket,
    HourlyUsage,
    MeasuredBy,
    month_bounds,
    usage_report,
)
from smarthome.shared.audit import AuditEvent

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
HOME = UUID("00000000-0000-0000-0000-00000000000a")
# Monday 2026-10-05 00:00 in São Paulo (UTC-3).
MONDAY = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
WEEKDAYS = frozenset(range(5))


def tariff(**overrides: object) -> Tariff:
    base = Tariff(
        home_id=HOME,
        currency="BRL",
        base_price=Decimal("0.80"),
        periods=(Period("ponta", WEEKDAYS, "18:00", "21:00", Decimal("1.60")),),
        monthly_budget_kwh=None,
        timezone="America/Sao_Paulo",
        version=1,
        updated_by="alice",
        updated_at=MONDAY,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def at(hours: int) -> datetime:
    return MONDAY + timedelta(hours=hours)


# --- Tariffs -------------------------------------------------------------------------


def test_the_period_price_applies_on_its_weekdays_and_hours_and_the_base_price_elsewhere() -> None:
    t = tariff()
    assert t.price_at(at(18).astimezone(SAO_PAULO)) == Decimal("1.60")
    assert t.price_at(at(20).astimezone(SAO_PAULO)) == Decimal("1.60")
    assert t.price_at(at(21).astimezone(SAO_PAULO)) == Decimal("0.80")  # end is exclusive
    assert t.price_at(at(17).astimezone(SAO_PAULO)) == Decimal("0.80")
    saturday_evening = at(5 * 24 + 19).astimezone(SAO_PAULO)
    assert saturday_evening.weekday() == 5
    assert t.price_at(saturday_evening) == Decimal("0.80")


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("18:30", "21:00", "whole hours"),
        ("22:00", "02:00", "no wrap"),
        ("24:00", "24:00", "no wrap"),
        ("7:00", "09:00", "whole hours"),
    ],
)
def test_periods_must_be_whole_hours_within_one_day(start: str, end: str, message: str) -> None:
    with pytest.raises(InvalidTariff, match=message):
        Period("p", WEEKDAYS, start, end, Decimal(1))


def test_a_period_may_end_at_midnight() -> None:
    late = Period("late", frozenset({6}), "21:00", "24:00", Decimal(1))
    sunday_2330 = datetime(2026, 10, 11, 23, 30, tzinfo=SAO_PAULO)
    assert late.covers(sunday_2330)


def test_overlapping_periods_on_a_shared_weekday_are_rejected() -> None:
    with pytest.raises(InvalidTariff, match="overlap"):
        tariff(
            periods=(
                Period("a", frozenset({0, 1}), "17:00", "20:00", Decimal(1)),
                Period("b", frozenset({1}), "19:00", "22:00", Decimal(2)),
            )
        )
    # Same hours on different days do not overlap.
    tariff(
        periods=(
            Period("a", frozenset({0}), "17:00", "20:00", Decimal(1)),
            Period("b", frozenset({1}), "17:00", "20:00", Decimal(2)),
        )
    )


def test_prices_are_parsed_exactly_and_floats_are_refused() -> None:
    assert parse_price("0.891234", field="p") == Decimal("0.891234")
    with pytest.raises(InvalidTariff, match="strings"):
        parse_price(0.1, field="p")
    with pytest.raises(InvalidTariff, match="decimal places"):
        parse_price("0.1234567", field="p")
    with pytest.raises(InvalidTariff, match="between"):
        parse_price("-1", field="p")
    with pytest.raises(InvalidTariff, match="not a number"):
        parse_price("abc", field="p")


def test_money_rounds_half_to_even_in_the_currency_minor_unit() -> None:
    assert round_money(Decimal("0.125"), "BRL") == Decimal("0.12")
    assert round_money(Decimal("0.135"), "BRL") == Decimal("0.14")
    assert round_money(Decimal("12.5"), "JPY") == Decimal("12")
    assert round_money(Decimal("1.2345"), "KWD") == Decimal("1.234")


def test_currency_and_budget_are_validated() -> None:
    with pytest.raises(InvalidTariff, match="ISO 4217"):
        tariff(currency="real")
    with pytest.raises(InvalidTariff, match="budget"):
        tariff(monthly_budget_kwh=Decimal(0))


# --- Usage report --------------------------------------------------------------------


def usage(device: str, hour: int, wh: float) -> HourlyUsage:
    return HourlyUsage(device, at(hour), wh)


def test_with_a_whole_home_meter_the_total_is_the_meter_and_plugs_are_a_breakdown() -> None:
    rows = [
        usage("meter", 0, 500.0),
        usage("fridge", 0, 120.0),
        usage("tv", 0, 80.0),
        usage("meter", 1, 300.0),
        usage("fridge", 1, 100.0),
    ]
    report = usage_report(
        rows,
        whole_home=frozenset({"meter"}),
        start=at(0),
        end=at(2),
        bucket=Bucket.HOUR,
        tz=SAO_PAULO,
        tariff=None,
    )
    assert report.measured_by is MeasuredBy.METER
    assert report.total_wh == 800.0
    assert report.unmetered_wh == 500.0  # 800 - (220 + 80)
    assert [b.wh for b in report.buckets] == [500.0, 300.0]
    assert [(d.device_id, d.wh, d.whole_home) for d in report.devices] == [
        ("meter", 800.0, True),
        ("fridge", 220.0, False),
        ("tv", 80.0, False),
    ]
    assert report.total_cost is None
    assert report.currency is None


def test_without_a_meter_the_total_is_the_sum_of_the_plugs() -> None:
    report = usage_report(
        [usage("fridge", 0, 120.0), usage("tv", 0, 80.0)],
        whole_home=frozenset({"meter"}),  # paired, but it reported nothing in the range
        start=at(0),
        end=at(1),
        bucket=Bucket.HOUR,
        tz=SAO_PAULO,
        tariff=None,
    )
    assert report.measured_by is MeasuredBy.SUBMETERS
    assert report.total_wh == 200.0
    assert report.unmetered_wh is None


def test_costs_use_the_price_of_each_hour_and_round_once_at_the_end() -> None:
    # 17:00 at base price, 18:00 at peak price. 1.333 kWh * 0.80 + 1.333 kWh * 1.60.
    rows = [usage("meter", 17, 1333.0), usage("meter", 18, 1333.0)]
    report = usage_report(
        rows,
        whole_home=frozenset({"meter"}),
        start=at(17),
        end=at(19),
        bucket=Bucket.HOUR,
        tz=SAO_PAULO,
        tariff=tariff(),
    )
    assert [b.cost for b in report.buckets] == [Decimal("1.07"), Decimal("2.13")]
    # 1.0664 + 2.1328 = 3.1992 -> 3.20; rounding each hour first would give 3.20 too,
    # but the total is computed from exact values, not from the rounded buckets.
    assert report.total_cost == Decimal("3.20")
    assert report.currency == "BRL"


def test_daily_buckets_follow_home_midnight_and_empty_days_are_zero() -> None:
    rows = [
        usage("meter", 0, 100.0),  # Monday 00:00 local
        usage("meter", 23, 50.0),  # Monday 23:00 local
        usage("meter", 24, 10.0),  # Tuesday 00:00 local
    ]
    report = usage_report(
        rows,
        whole_home=frozenset({"meter"}),
        start=at(0),
        end=at(72),
        bucket=Bucket.DAY,
        tz=SAO_PAULO,
        tariff=None,
    )
    assert [(b.start.day, b.wh) for b in report.buckets] == [(5, 150.0), (6, 10.0), (7, 0.0)]
    assert all(b.start.hour == 0 and b.start.tzinfo is SAO_PAULO for b in report.buckets)


def test_rows_outside_the_range_are_ignored() -> None:
    report = usage_report(
        [usage("meter", -1, 999.0), usage("meter", 0, 1.0), usage("meter", 5, 999.0)],
        whole_home=frozenset({"meter"}),
        start=at(0),
        end=at(1),
        bucket=Bucket.HOUR,
        tz=SAO_PAULO,
        tariff=None,
    )
    assert report.total_wh == 1.0


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (at(1), at(0), "before"),
        (at(0), at(0) + timedelta(days=401), "400 days"),
        (at(0).replace(tzinfo=None), at(1), "UTC offset"),
    ],
)
def test_ranges_are_validated(start: datetime, end: datetime, message: str) -> None:
    with pytest.raises(InvalidRange, match=message):
        usage_report(
            [],
            whole_home=frozenset(),
            start=start,
            end=end,
            bucket=Bucket.DAY,
            tz=SAO_PAULO,
            tariff=None,
        )


def test_the_budget_month_starts_at_local_midnight_on_the_first() -> None:
    now = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)  # still October 31st in São Paulo
    start, end = month_bounds(now, SAO_PAULO)
    assert (start.year, start.month, start.day, start.hour) == (2026, 10, 1, 0)
    assert end == now


# --- Services with fakes -------------------------------------------------------------


@dataclass
class FakeStore:
    tariffs: dict[UUID, Tariff] = field(default_factory=dict)
    hourly: dict[tuple[str, datetime], Increase] = field(default_factory=dict)
    cursor: datetime | None = None
    locked: bool = False
    audit: list[AuditEvent] = field(default_factory=list)


class FakeHourly:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.pending: dict[tuple[str, datetime], Increase] = {}

    async def upsert(self, rows: list[Increase]) -> int:
        for row in rows:
            self.pending[(row.device_id, row.hour)] = row
        return len(rows)

    async def for_home(self, home_id: UUID, *, start: datetime, end: datetime) -> list[HourlyUsage]:
        return [
            HourlyUsage(r.device_id, r.hour, r.wh)
            for r in self.store.hourly.values()
            if r.home_id == home_id and start <= r.hour < end
        ]


class FakeCursor:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.pending: datetime | None = None
        self.holds_lock = False

    async def try_lock(self) -> bool:
        if self.store.locked:
            return False
        self.store.locked = self.holds_lock = True
        return True

    async def get(self) -> datetime | None:
        return self.store.cursor

    async def advance(self, until: datetime) -> None:
        self.pending = until


class FakeTariffs:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.pending: dict[UUID, Tariff] = {}

    async def get(self, home_id: UUID) -> Tariff | None:
        return self.store.tariffs.get(home_id)

    async def lock(self, home_id: UUID) -> Tariff | None:
        return self.store.tariffs.get(home_id)

    async def save(self, tariff: Tariff) -> None:
        self.pending[tariff.home_id] = tariff

    async def with_budget(self) -> list[Tariff]:
        return [t for t in self.store.tariffs.values() if t.monthly_budget_kwh is not None]


class FakeAudit:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None:
        self.store.audit.append(event)


class FakeUnitOfWork:
    """Writes become visible on commit; the rollup lock is released when the unit ends."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.hourly = FakeHourly(store)
        self.cursor = FakeCursor(store)
        self.tariffs = FakeTariffs(store)
        self.audit = FakeAudit(store)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.cursor.holds_lock:
            self.store.locked = False

    async def commit(self) -> None:
        self.store.tariffs |= self.tariffs.pending
        self.store.hourly |= self.hourly.pending
        if self.cursor.pending is not None:
            self.store.cursor = self.cursor.pending


class Clock:
    def __init__(self, now: datetime) -> None:
        self.at = now

    def now(self) -> datetime:
        return self.at


class Devices:
    async def metered(self, home_id: UUID) -> list[MeteredDevice]:
        return [MeteredDevice("meter", "Meter", "utility", whole_home=True)]


class Counters:
    def __init__(self) -> None:
        self.calls: list[tuple[datetime, datetime]] = []

    async def hourly_increase(self, *, start: datetime, end: datetime) -> list[Increase]:
        self.calls.append((start, end))
        return [Increase(HOME, "meter", start, 100.0)]


async def set_tariff(
    service: EnergyService, *, expected_version: int | None, budget: Decimal | None = None
) -> Tariff:
    return await service.set_tariff(
        HOME,
        timezone="America/Sao_Paulo",
        currency="BRL",
        base_price=Decimal("0.80"),
        periods=(),
        monthly_budget_kwh=budget,
        expected_version=expected_version,
        actor="alice",
    )


async def test_the_first_tariff_needs_no_version_and_later_edits_must_name_the_current_one() -> (
    None
):
    store = FakeStore()
    service = EnergyService(lambda: FakeUnitOfWork(store), Devices(), Clock(MONDAY))

    first = await set_tariff(service, expected_version=None)
    assert first.version == 1
    with pytest.raises(VersionRequired):
        await set_tariff(service, expected_version=None)
    with pytest.raises(VersionMismatch):
        await set_tariff(service, expected_version=7)
    second = await set_tariff(service, expected_version=1)
    assert second.version == 2
    assert [e.action for e in store.audit] == ["energy.tariff_set"] * 2
    assert store.audit[0].tenant_id == HOME


async def test_budgets_report_month_to_date_use_against_the_budget() -> None:
    store = FakeStore()
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    service = EnergyService(lambda: FakeUnitOfWork(store), Devices(), Clock(now))
    await set_tariff(service, expected_version=None, budget=Decimal(100))
    store.hourly[("meter", now - timedelta(days=2))] = Increase(
        HOME, "meter", now - timedelta(days=2), 40_000.0
    )
    store.hourly[("meter", now - timedelta(days=20))] = Increase(  # last month
        HOME, "meter", now - timedelta(days=20), 99_000.0
    )

    [status] = await service.budgets()
    assert status.home_id == HOME
    assert status.used_kwh == Decimal(40)
    assert status.ratio == Decimal("0.4")


async def test_the_rollup_recomputes_recent_hours_in_day_chunks_and_advances_its_cursor() -> None:
    store = FakeStore()
    now = datetime(2026, 10, 10, 12, 30, tzinfo=UTC)
    clock = Clock(now)
    counters = Counters()
    rollup = EnergyRollup(lambda: FakeUnitOfWork(store), counters, clock)

    await rollup.run_once()
    # First run: a week back (minus the recompute margin), in 24 h chunks, up to now.
    assert counters.calls[0][0] == datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
    assert counters.calls[-1][1] == now
    assert all(end - start <= timedelta(hours=24) for start, end in counters.calls)
    assert store.cursor == now

    counters.calls.clear()
    clock.at = now + timedelta(minutes=1)
    await rollup.run_once()
    # Later runs start two hours before the cursor, on an hour boundary.
    assert counters.calls == [(datetime(2026, 10, 10, 10, 0, tzinfo=UTC), clock.at)]


async def test_a_rollup_that_cannot_take_the_lock_skips_the_run() -> None:
    store = FakeStore(locked=True)
    counters = Counters()
    rollup = EnergyRollup(lambda: FakeUnitOfWork(store), counters, Clock(MONDAY))
    assert await rollup.run_once() == 0
    assert counters.calls == []
    assert store.cursor is None
