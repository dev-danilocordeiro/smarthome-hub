"""Energy over real TimescaleDB: counter deltas with resets, the rollup, tariffs and the
usage API."""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Self
from uuid import UUID

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.energy import wiring as energy
from smarthome.modules.energy.application.services import EnergyService
from smarthome.modules.energy.domain.errors import VersionRequired
from smarthome.modules.energy.domain.tariff import Tariff
from smarthome.modules.energy.infrastructure.modules import MeteredDevices
from smarthome.modules.energy.infrastructure.persistence import (
    PostgresEnergyUnitOfWork,
    PostgresTariffs,
)
from smarthome.modules.identity.domain.model import Role
from smarthome.modules.telemetry import wiring as telemetry
from smarthome.modules.telemetry.domain.model import Reading
from smarthome.modules.telemetry.infrastructure.timescale import TimescaleReadings
from smarthome.shared.config import Settings
from tests.integration.energy.conftest import Household
from tests.integration.helpers import Member, sign_in

# A fixed past instant: Monday 2026-09-07 00:00 in São Paulo.
T0 = datetime(2026, 9, 7, 3, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.at = now

    def now(self) -> datetime:
        return self.at


async def counter(
    engine: AsyncEngine, household: Household, device: str, samples: list[tuple[int, float]]
) -> None:
    """`samples` are (minutes after T0, counter value in Wh)."""
    await TimescaleReadings(engine).write(
        [
            Reading(
                time=T0 + timedelta(minutes=minute),
                home_id=household.home,
                device_id=household.devices[device],
                metric="energy_wh_total",
                value=value,
                message_id=secrets.token_hex(8),
                received_at=T0 + timedelta(minutes=minute),
            )
            for minute, value in samples
        ]
    )


async def hourly(engine: AsyncEngine, household: Household) -> dict[tuple[str, int], float]:
    """(device name, hours after T0) -> Wh, from energy.hourly."""
    names = {v: k for k, v in household.devices.items()}
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT device_id, hour, wh FROM energy.hourly WHERE home_id = :home"),
            {"home": household.home},
        )
        return {
            (names[r.device_id], int((r.hour - T0).total_seconds() // 3600)): r.wh for r in rows
        }


async def test_counter_increase_sums_deltas_per_hour_and_treats_a_drop_as_a_reboot(
    engine: AsyncEngine, redis: Redis, household: Household
) -> None:
    await counter(
        engine,
        household,
        "fridge",
        [
            (-30, 1000.0),  # before the window: only the predecessor of the first delta
            (10, 1100.0),  # +100 in hour 0
            (50, 1150.0),  # +50 in hour 0
            (70, 20.0),  # rebooted: counted from zero, +20 in hour 1
            (110, 70.0),  # +50 in hour 1
        ],
    )
    queries = telemetry.build_queries(engine=engine, redis=redis)

    rows = await queries.hourly_increase(
        metric="energy_wh_total", start=T0, end=T0 + timedelta(hours=2)
    )

    mine = {(r.hour, r.increase) for r in rows if r.device_id == household.devices["fridge"]}
    assert mine == {(T0, 150.0), (T0 + timedelta(hours=1), 70.0)}


@pytest.mark.usefixtures("fresh_rollup")
async def test_the_rollup_is_idempotent_and_picks_up_late_readings_in_recent_hours(
    engine: AsyncEngine, redis: Redis, household: Household
) -> None:
    await counter(engine, household, "meter", [(0, 0.0), (30, 400.0), (90, 900.0)])
    await counter(engine, household, "fridge", [(0, 0.0), (30, 100.0)])
    clock = FixedClock(T0 + timedelta(hours=2))
    rollup = energy.build_rollup(
        engine=engine, telemetry=telemetry.build_queries(engine=engine, redis=redis), clock=clock
    )

    await rollup.run_once()
    first = await hourly(engine, household)
    assert first == {("meter", 0): 400.0, ("meter", 1): 500.0, ("fridge", 0): 100.0}

    clock.at += timedelta(minutes=1)
    assert await rollup.run_once() == 0  # nothing changed, nothing rewritten
    assert await hourly(engine, household) == first

    # A reading that arrives late for hour 1 is folded in by the next run.
    await counter(engine, household, "fridge", [(75, 160.0)])
    clock.at += timedelta(minutes=1)
    await rollup.run_once()
    assert (await hourly(engine, household))[("fridge", 1)] == 60.0


@pytest.mark.usefixtures("fresh_rollup")
async def test_a_rollup_skips_its_run_while_another_worker_holds_the_lock(
    engine: AsyncEngine, redis: Redis, household: Household
) -> None:
    await counter(engine, household, "meter", [(0, 0.0), (30, 400.0)])
    rollup = energy.build_rollup(
        engine=engine,
        telemetry=telemetry.build_queries(engine=engine, redis=redis),
        clock=FixedClock(T0 + timedelta(hours=1)),
    )
    async with engine.connect() as other_worker, other_worker.begin():
        await other_worker.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('energy:rollup', 0))")
        )
        assert await rollup.run_once() == 0
        assert await hourly(engine, household) == {}
    assert await rollup.run_once() == 1


class SlowTariffs(PostgresTariffs):
    """Holds the transaction open after reading, so a second save reads before the first
    one writes, unless the lock makes it wait."""

    async def lock(self, home_id: UUID) -> Tariff | None:
        found = await super().lock(home_id)
        await asyncio.sleep(0.5)
        return found


class SlowUnitOfWork(PostgresEnergyUnitOfWork):
    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self.tariffs = SlowTariffs(self.tariffs._conn)
        return self


async def test_two_first_saves_of_a_tariff_at_once_do_not_both_win(
    engine: AsyncEngine, household: Household, device_service: DevicesService
) -> None:
    service = EnergyService(
        lambda: SlowUnitOfWork(engine), MeteredDevices(device_service), FixedClock(T0)
    )

    async def save(price: str) -> int:
        tariff = await service.set_tariff(
            household.home,
            timezone="America/Sao_Paulo",
            currency="BRL",
            base_price=Decimal(price),
            periods=(),
            monthly_budget_kwh=None,
            expected_version=None,
            actor="alice",
        )
        return tariff.version

    results = await asyncio.gather(save("0.80"), save("0.90"), return_exceptions=True)

    assert sorted(type(r).__name__ for r in results) == ["VersionRequired", "int"]
    assert [r for r in results if not isinstance(r, VersionRequired)] == [1]


# --- HTTP ----------------------------------------------------------------------------


async def member(redis: Redis, household: Household, role: Role, **invite: object) -> Member:
    joined = await sign_in(redis, f"{role.value}-{secrets.token_hex(4)}")
    _, token = await household.identity.invite(
        household.owner.principal,
        household.home,
        role=role,
        **invite,  # type: ignore[arg-type]
    )
    await household.identity.accept_invitation(joined.principal, token)
    return joined


async def test_a_tariff_is_created_then_edited_with_if_match(
    api: httpx.AsyncClient, household: Household
) -> None:
    url = f"/homes/{household.home}/energy/tariff"
    headers = household.owner.headers
    body = {
        "currency": "BRL",
        "base_price": "0.656",
        "periods": [
            {
                "name": "ponta",
                "weekdays": [0, 1, 2, 3, 4],
                "start": "18:00",
                "end": "21:00",
                "price": "1.432551",
            }
        ],
        "monthly_budget_kwh": "250",
    }

    assert (await api.get(url, headers=headers)).status_code == 404
    created = await api.put(url, json=body, headers=headers)
    assert created.status_code == 200, created.text
    assert created.headers["etag"] == '"1"'
    assert created.json()["periods"][0]["price"] == "1.432551"
    assert created.json()["timezone"] == "America/Sao_Paulo"

    missing = await api.put(url, json=body, headers=headers)
    stale = await api.put(url, json=body, headers=headers | {"if-match": '"9"'})
    ok = await api.put(
        url, json=body | {"base_price": "0.7"}, headers=headers | {"if-match": '"1"'}
    )
    assert missing.status_code == 428
    assert stale.status_code == 412
    assert ok.status_code == 200
    assert ok.headers["etag"] == '"2"'
    fetched = await api.get(url, headers=headers)
    assert fetched.json()["base_price"] == "0.700000"
    assert fetched.headers["etag"] == '"2"'


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"base_price": 0.8}, 422),  # a float, not a decimal string
        ({"base_price": "0.1234567"}, 422),
        (
            {
                "periods": [
                    {"name": "x", "weekdays": [0], "start": "18:30", "end": "21:00", "price": "1"}
                ]
            },
            422,
        ),
        ({"currency": "brl"}, 422),
    ],
)
async def test_invalid_tariffs_are_rejected(
    api: httpx.AsyncClient, household: Household, change: dict[str, object], status: int
) -> None:
    body = {"currency": "BRL", "base_price": "0.8"} | change
    response = await api.put(
        f"/homes/{household.home}/energy/tariff", json=body, headers=household.owner.headers
    )
    assert response.status_code == status, response.text


@pytest.mark.usefixtures("fresh_rollup")
async def test_usage_reports_the_meter_total_a_per_device_breakdown_and_cost(
    api: httpx.AsyncClient,
    engine: AsyncEngine,
    redis: Redis,
    household: Household,
    migrated_database: Settings,
) -> None:
    await counter(engine, household, "meter", [(0, 0.0), (30, 1000.0), (90, 3000.0)])
    await counter(engine, household, "fridge", [(0, 0.0), (90, 500.0)])
    await energy.build_rollup(
        engine=engine,
        telemetry=telemetry.build_queries(engine=engine, redis=redis),
        clock=FixedClock(T0 + timedelta(hours=3)),
    ).run_once()
    headers = household.owner.headers
    await api.put(
        f"/homes/{household.home}/energy/tariff",
        json={"currency": "BRL", "base_price": "0.80"},
        headers=headers,
    )

    response = await api.get(
        f"/homes/{household.home}/energy/usage",
        params={
            "from": T0.isoformat(),
            "to": (T0 + timedelta(hours=3)).isoformat(),
            "bucket": "hour",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    usage = response.json()
    assert usage["measured_by"] == "meter"
    assert usage["total_wh"] == 3000.0
    assert usage["total_cost"] == "2.40"
    assert usage["unmetered_wh"] == 2500.0
    assert [b["wh"] for b in usage["buckets"]] == [1000.0, 2000.0, 0.0]
    assert usage["buckets"][0]["start"].startswith("2026-09-07T00:00:00-03:00")
    assert [(d["name"], d["wh"]) for d in usage["devices"]] == [
        ("Meter", 3000.0),
        ("Fridge", 500.0),
    ]
    assert usage["from"] == "2026-09-07T03:00:00Z"


async def test_scoped_guests_cannot_see_home_energy_and_viewers_cannot_set_tariffs(
    api: httpx.AsyncClient, redis: Redis, household: Household
) -> None:
    guest = await member(
        redis,
        household,
        Role.GUEST,
        device_scope=frozenset({household.devices["hall"]}),
        guest_access_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    viewer = await member(redis, household, Role.VIEWER)
    usage = f"/homes/{household.home}/energy/usage"
    params = {"from": T0.isoformat(), "to": (T0 + timedelta(days=1)).isoformat()}

    assert (await api.get(usage, params=params, headers=guest.headers)).status_code == 403
    assert (await api.get(usage, params=params, headers=viewer.headers)).status_code == 200
    put = await api.put(
        f"/homes/{household.home}/energy/tariff",
        json={"currency": "BRL", "base_price": "0.8"},
        headers=viewer.headers,
    )
    assert put.status_code == 403
