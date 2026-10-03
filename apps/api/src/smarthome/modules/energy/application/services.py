from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from opentelemetry import metrics

from smarthome.modules.energy.application.ports import (
    CounterSource,
    DeviceDirectory,
    EnergyUnitOfWorkFactory,
    MeteredDevice,
)
from smarthome.modules.energy.domain.errors import VersionMismatch, VersionRequired
from smarthome.modules.energy.domain.tariff import Period, Tariff
from smarthome.modules.energy.domain.usage import (
    Bucket,
    BudgetStatus,
    UsageReport,
    floor_hour,
    home_total_wh,
    month_bounds,
    usage_report,
    validate_range,
)
from smarthome.shared.audit import AuditEvent
from smarthome.shared.clock import Clock

log = structlog.get_logger(__name__)
meter = metrics.get_meter(__name__)

rollup_rows = meter.create_counter(
    "smarthome.energy.rollup.rows",
    unit="{row}",
    description="Hourly consumption rows written (inserted or recomputed) by the rollup.",
)
rollup_lag = meter.create_histogram(
    "smarthome.energy.rollup.lag",
    unit="s",
    description="How far behind now the rollup cursor was when a run started.",
)


class EnergyRollup:
    """Folds counter readings into `energy.hourly`, idempotently.

    Each run recomputes from a little before the cursor (late or redelivered telemetry
    changes recent hours) up to now, in day-sized chunks, and replaces what was there.
    Running it twice, or on two workers at once, changes nothing: one of them gets the
    lock, the other skips the run.
    """

    def __init__(
        self,
        uow: EnergyUnitOfWorkFactory,
        source: CounterSource,
        clock: Clock,
        *,
        recompute: timedelta = timedelta(hours=2),
        first_run: timedelta = timedelta(days=7),
        chunk: timedelta = timedelta(hours=24),
    ) -> None:
        self._uow = uow
        self._source = source
        self._clock = clock
        self._recompute = recompute
        self._first_run = first_run
        self._chunk = chunk

    async def run_once(self) -> int:
        now = self._clock.now()
        written = 0
        async with self._uow() as uow:
            if not await uow.cursor.try_lock():
                return 0
            cursor = await uow.cursor.get()
            if cursor is not None:
                rollup_lag.record((now - cursor).total_seconds())
            start = floor_hour((cursor or now - self._first_run) - self._recompute)
            while start < now:
                end = min(start + self._chunk, now)
                rows = await self._source.hourly_increase(start=start, end=end)
                written += await uow.hourly.upsert(rows)
                start = end
            await uow.cursor.advance(now)
            await uow.commit()
        rollup_rows.add(written)
        return written


class EnergyService:
    def __init__(
        self, uow: EnergyUnitOfWorkFactory, devices: DeviceDirectory, clock: Clock
    ) -> None:
        self._uow = uow
        self._devices = devices
        self._clock = clock

    async def usage(
        self, home_id: UUID, *, timezone: str, start: datetime, end: datetime, bucket: Bucket
    ) -> tuple[UsageReport, list[MeteredDevice]]:
        validate_range(start, end)
        devices = await self._devices.metered(home_id)
        async with self._uow() as uow:
            rows = await uow.hourly.for_home(home_id, start=floor_hour(start), end=end)
            tariff = await uow.tariffs.get(home_id)
        report = usage_report(
            rows,
            whole_home=frozenset(d.id for d in devices if d.whole_home),
            start=start,
            end=end,
            bucket=bucket,
            tz=ZoneInfo(timezone),
            tariff=tariff,
        )
        return report, devices

    async def tariff(self, home_id: UUID) -> Tariff | None:
        async with self._uow() as uow:
            return await uow.tariffs.get(home_id)

    async def set_tariff(
        self,
        home_id: UUID,
        *,
        timezone: str,
        currency: str,
        base_price: Decimal,
        periods: tuple[Period, ...],
        monthly_budget_kwh: Decimal | None,
        expected_version: int | None,
        actor: str,
    ) -> Tariff:
        now = self._clock.now()
        async with self._uow() as uow:
            current = await uow.tariffs.lock(home_id)
            if current is not None:
                if expected_version is None:
                    raise VersionRequired
                if expected_version != current.version:
                    raise VersionMismatch(f"current version is {current.version}")
            tariff = Tariff(
                home_id=home_id,
                currency=currency,
                base_price=base_price,
                periods=periods,
                monthly_budget_kwh=monthly_budget_kwh,
                timezone=timezone,
                version=current.version + 1 if current else 1,
                updated_by=actor,
                updated_at=now,
            )
            await uow.tariffs.save(tariff)
            await uow.audit.append(
                AuditEvent(
                    actor=actor,
                    action="energy.tariff_set",
                    target_type="home",
                    target_id=str(home_id),
                    tenant_id=home_id,
                    details={
                        "version": tariff.version,
                        "currency": currency,
                        "base_price": str(base_price),
                        "periods": len(periods),
                        "monthly_budget_kwh": (
                            str(monthly_budget_kwh) if monthly_budget_kwh is not None else None
                        ),
                    },
                ),
                occurred_at=now,
            )
            await uow.commit()
        return tariff

    async def budgets(self) -> list[BudgetStatus]:
        """Month-to-date use of every home that has set a monthly budget."""
        now = self._clock.now()
        async with self._uow() as uow:
            tariffs = await uow.tariffs.with_budget()
        found = []
        for tariff in tariffs:
            if tariff.monthly_budget_kwh is None:
                continue
            start, end = month_bounds(now, ZoneInfo(tariff.timezone))
            devices = await self._devices.metered(tariff.home_id)
            async with self._uow() as uow:
                rows = await uow.hourly.for_home(tariff.home_id, start=start, end=end)
            used_wh = home_total_wh(
                rows, whole_home=frozenset(d.id for d in devices if d.whole_home)
            )
            found.append(
                BudgetStatus(
                    home_id=tariff.home_id,
                    month_start=start,
                    used_kwh=Decimal(str(round(used_wh, 3))) / 1000,
                    budget_kwh=tariff.monthly_budget_kwh,
                )
            )
        return found
