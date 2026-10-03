"""Postgres adapters for the energy ports (schema `energy`)."""

import json
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from smarthome.modules.energy.application.ports import Increase
from smarthome.modules.energy.domain.tariff import Period, Tariff
from smarthome.modules.energy.domain.usage import HourlyUsage
from smarthome.shared.infrastructure.audit import PostgresAuditLog

# Recomputed hours replace what was there: the rollup always computes a whole hour.
UPSERT_HOURLY = text(
    "INSERT INTO energy.hourly (device_id, hour, home_id, wh, updated_at)"
    " SELECT d, h, home, wh, now() FROM unnest("
    " CAST(:devices AS text[]), CAST(:hours AS timestamptz[]), CAST(:homes AS uuid[]),"
    " CAST(:whs AS double precision[])) AS t(d, h, home, wh)"
    " ON CONFLICT (device_id, hour) DO UPDATE SET wh = EXCLUDED.wh, updated_at = now()"
    " WHERE energy.hourly.wh IS DISTINCT FROM EXCLUDED.wh"
)
TARIFF_COLUMNS = (
    "home_id, currency, base_price, periods, monthly_budget_kwh, timezone, version,"
    " updated_by, updated_at"
)
ROLLUP_LOCK = "energy:rollup"


class PostgresHourly:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def upsert(self, rows: list[Increase]) -> int:
        if not rows:
            return 0
        result = await self._conn.execute(
            UPSERT_HOURLY,
            {
                "devices": [r.device_id for r in rows],
                "hours": [r.hour for r in rows],
                "homes": [r.home_id for r in rows],
                "whs": [max(0.0, r.wh) for r in rows],
            },
        )
        return int(result.rowcount)

    async def for_home(self, home_id: UUID, *, start: datetime, end: datetime) -> list[HourlyUsage]:
        rows = await self._conn.execute(
            text(
                "SELECT device_id, hour, wh FROM energy.hourly"
                " WHERE home_id = :home AND hour >= :start AND hour < :end"
            ),
            {"home": home_id, "start": start, "end": end},
        )
        return [HourlyUsage(r.device_id, r.hour, r.wh) for r in rows]


class PostgresRollupCursor:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def try_lock(self) -> bool:
        return bool(
            await self._conn.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": ROLLUP_LOCK},
            )
        )

    async def get(self) -> datetime | None:
        found: datetime | None = await self._conn.scalar(
            text("SELECT computed_until FROM energy.rollup_cursor")
        )
        return found

    async def advance(self, until: datetime) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO energy.rollup_cursor (id, computed_until) VALUES (true, :until)"
                " ON CONFLICT (id) DO UPDATE SET computed_until = EXCLUDED.computed_until"
            ),
            {"until": until},
        )


def _tariff(row: Row[Any]) -> Tariff:
    return Tariff(
        home_id=row.home_id,
        currency=row.currency,
        base_price=Decimal(row.base_price),
        periods=tuple(
            Period(
                name=p["name"],
                weekdays=frozenset(p["weekdays"]),
                start=p["start"],
                end=p["end"],
                price=Decimal(p["price"]),
            )
            for p in row.periods
        ),
        monthly_budget_kwh=(
            Decimal(row.monthly_budget_kwh) if row.monthly_budget_kwh is not None else None
        ),
        timezone=row.timezone,
        version=row.version,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


class PostgresTariffs:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get(self, home_id: UUID) -> Tariff | None:
        row = (
            await self._conn.execute(
                text(f"SELECT {TARIFF_COLUMNS} FROM energy.tariffs WHERE home_id = :home"),  # noqa: S608
                {"home": home_id},
            )
        ).first()
        return _tariff(row) if row else None

    async def lock(self, home_id: UUID) -> Tariff | None:
        # Serialises the first save too: two creates for a home would both see no row.
        await self._conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"energy:tariff:{home_id}"},
        )
        return await self.get(home_id)

    async def save(self, tariff: Tariff) -> None:
        await self._conn.execute(
            text(
                f"INSERT INTO energy.tariffs ({TARIFF_COLUMNS})"  # noqa: S608
                " VALUES (:home, :currency, :base_price, CAST(:periods AS jsonb), :budget,"
                " :timezone, :version, :updated_by, :updated_at)"
                " ON CONFLICT (home_id) DO UPDATE SET currency = EXCLUDED.currency,"
                " base_price = EXCLUDED.base_price, periods = EXCLUDED.periods,"
                " monthly_budget_kwh = EXCLUDED.monthly_budget_kwh,"
                " timezone = EXCLUDED.timezone, version = EXCLUDED.version,"
                " updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at"
            ),
            {
                "home": tariff.home_id,
                "currency": tariff.currency,
                "base_price": tariff.base_price,
                "periods": json.dumps(
                    [
                        {
                            "name": p.name,
                            "weekdays": sorted(p.weekdays),
                            "start": p.start,
                            "end": p.end,
                            "price": str(p.price),
                        }
                        for p in tariff.periods
                    ]
                ),
                "budget": tariff.monthly_budget_kwh,
                "timezone": tariff.timezone,
                "version": tariff.version,
                "updated_by": tariff.updated_by,
                "updated_at": tariff.updated_at,
            },
        )

    async def with_budget(self) -> list[Tariff]:
        rows = await self._conn.execute(
            text(
                f"SELECT {TARIFF_COLUMNS} FROM energy.tariffs"  # noqa: S608
                " WHERE monthly_budget_kwh IS NOT NULL"
            )
        )
        return [_tariff(r) for r in rows]


class PostgresEnergyUnitOfWork:
    hourly: PostgresHourly
    cursor: PostgresRollupCursor
    tariffs: PostgresTariffs
    audit: PostgresAuditLog

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn: AsyncConnection | None = None
        self._tx: AsyncTransaction | None = None

    async def __aenter__(self) -> Self:
        self._conn = await self._engine.connect()
        self._tx = await self._conn.begin()
        self.hourly = PostgresHourly(self._conn)
        self.cursor = PostgresRollupCursor(self._conn)
        self.tariffs = PostgresTariffs(self._conn)
        self.audit = PostgresAuditLog(self._conn)
        return self

    async def commit(self) -> None:
        if self._tx is None:
            raise RuntimeError("unit of work is not active")
        await self._tx.commit()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._tx is not None and self._tx.is_active:
                await self._tx.rollback()
        finally:
            if self._conn is not None:
                await self._conn.close()
            self._conn = None
            self._tx = None
