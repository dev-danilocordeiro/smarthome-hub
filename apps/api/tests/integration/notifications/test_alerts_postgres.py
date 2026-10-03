"""Alerts and notifications over real Postgres: transitions, fan-out, deduplication under
concurrency, stale events, grace periods, budgets, and delivery to Mailpit and a webhook."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.energy import wiring as energy
from smarthome.modules.notifications.application.alerts import AlertEngine
from smarthome.modules.notifications.application.dispatch import DeliveryDispatcher
from smarthome.modules.notifications.domain.webhook import SIGNATURE_HEADER, verify
from smarthome.shared.events import DeviceEvent, DeviceEventKind
from smarthome.shared.infrastructure.event_stream import RedisEventPublisher, StreamConsumer
from tests.integration.helpers import eventually
from tests.integration.notifications.conftest import (
    Household,
    MovableClock,
    Receiver,
    mailbox,
)


def battery(household: Household, pct: float, at: datetime) -> DeviceEvent:
    return DeviceEvent(
        household.home,
        household.devices["sensor"],
        DeviceEventKind.TELEMETRY,
        at,
        {"battery_pct": pct, "rssi_dbm": -60.0},
    )


def presence(household: Household, device: str, online: bool, at: datetime) -> DeviceEvent:
    return DeviceEvent(
        household.home, household.devices[device], DeviceEventKind.PRESENCE, at, {"online": online}
    )


async def rows(engine: AsyncEngine, query: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list(await conn.execute(text(query), params))


async def alerts_of(engine: AsyncEngine, household: Household) -> list[Any]:
    return await rows(
        engine,
        "SELECT key, status, severity, opened_at FROM notifications.alerts"
        " WHERE home_id = :home ORDER BY created_at",
        home=household.home,
    )


async def inbox_of(engine: AsyncEngine, household: Household) -> list[tuple[str, str, str]]:
    found = await rows(
        engine,
        "SELECT user_id, event, title FROM notifications.inbox WHERE home_id = :home"
        " ORDER BY created_at, user_id",
        home=household.home,
    )
    return [(r.user_id, r.event, r.title) for r in found]


async def deliveries_of(engine: AsyncEngine, household: Household) -> list[Any]:
    return await rows(
        engine,
        "SELECT channel, target, status, attempts, last_error, next_attempt_at"
        " FROM notifications.deliveries WHERE home_id = :home ORDER BY id",
        home=household.home,
    )


async def test_a_low_battery_opens_one_alert_tells_every_member_but_guests_and_resolves(
    engine: AsyncEngine, household: Household, alert_engine: AlertEngine, clock: MovableClock
) -> None:
    t0 = clock.now()
    assert await alert_engine.handle_event("1-0", battery(household, 12, t0)) == 1
    assert (
        await alert_engine.handle_event("2-0", battery(household, 11, t0 + timedelta(minutes=1)))
        == 0
    )

    [alert] = await alerts_of(engine, household)
    assert (alert.key, alert.status, alert.severity) == (
        f"low_battery:{household.devices['sensor']}",
        "open",
        "warning",
    )
    title = "Sensor battery is low (12%)"
    members = sorted(
        m.principal.user_id for m in (household.owner, household.resident, household.viewer)
    )
    assert await inbox_of(engine, household) == [(m, "opened", title) for m in members]
    # Email only for members the IdP gave an address (the viewer has none).
    emails = await deliveries_of(engine, household)
    assert sorted(d.target for d in emails) == sorted(
        f"{m.principal.user_id}@example.com" for m in (household.owner, household.resident)
    )
    assert {d.status for d in emails} == {"queued"}

    clock.at += timedelta(minutes=5)
    assert await alert_engine.handle_event("3-0", battery(household, 17, clock.now())) == 0
    assert await alert_engine.handle_event("4-0", battery(household, 95, clock.now())) == 1
    [resolved] = await alerts_of(engine, household)
    assert resolved.status == "resolved"
    resolved_entries = [e for e in await inbox_of(engine, household) if e[1] == "resolved"]
    assert len(resolved_entries) == 3
    assert len(await deliveries_of(engine, household)) == 2  # no email for a resolution


async def test_workers_racing_on_the_same_event_open_the_alert_once(
    engine: AsyncEngine, household: Household, alert_engine: AlertEngine, clock: MovableClock
) -> None:
    event = battery(household, 5, clock.now())

    opened = await asyncio.gather(*(alert_engine.handle_event(f"{i}-0", event) for i in range(8)))

    assert sum(opened) == 1
    assert len(await alerts_of(engine, household)) == 1
    assert len(await inbox_of(engine, household)) == 3
    assert len(await deliveries_of(engine, household)) == 2


async def test_an_event_older_than_the_alert_cannot_resolve_it(
    engine: AsyncEngine, household: Household, alert_engine: AlertEngine, clock: MovableClock
) -> None:
    t0 = clock.now()
    await alert_engine.handle_event("1-0", battery(household, 10, t0))
    late_but_old = battery(household, 80, t0 - timedelta(minutes=10))

    assert await alert_engine.handle_event("2-0", late_but_old) == 0
    assert [a.status for a in await alerts_of(engine, household)] == ["open"]


async def test_an_offline_device_alerts_only_after_the_grace_period_if_still_offline(
    *,
    engine: AsyncEngine,
    household: Household,
    alert_engine: AlertEngine,
    device_service: DevicesService,
    clock: MovableClock,
) -> None:
    t0 = clock.now()
    lock = household.devices["lock"]
    await device_service.record_presence(
        home_id=household.home, device_id=lock, online=False, at=t0
    )
    await alert_engine.handle_event("1-0", presence(household, "lock", False, t0))
    [pending] = await alerts_of(engine, household)
    assert pending.status == "pending"
    assert await inbox_of(engine, household) == []

    clock.at = t0 + timedelta(seconds=30)
    assert await alert_engine.fire_due() == 0
    clock.at = t0 + timedelta(seconds=61)
    assert await alert_engine.fire_due() == 1
    [opened] = await alerts_of(engine, household)
    assert (opened.status, opened.severity) == ("open", "critical")
    assert len(await inbox_of(engine, household)) == 3


async def test_a_device_back_within_the_grace_period_never_alerts_even_if_the_event_was_lost(
    *,
    engine: AsyncEngine,
    household: Household,
    alert_engine: AlertEngine,
    device_service: DevicesService,
    clock: MovableClock,
) -> None:
    t0 = clock.now()
    sensor = household.devices["sensor"]
    await alert_engine.handle_event("1-0", presence(household, "sensor", False, t0))
    # Back online, but the `online` event never reached the alert engine.
    await device_service.record_presence(
        home_id=household.home, device_id=sensor, online=True, at=t0 + timedelta(seconds=20)
    )

    clock.at = t0 + timedelta(minutes=2)
    assert await alert_engine.fire_due() == 1  # dropped, not opened
    [dropped] = await alerts_of(engine, household)
    assert (dropped.status, dropped.opened_at) == ("resolved", None)
    assert await inbox_of(engine, household) == []


async def test_budget_alerts_open_once_per_threshold_and_month(
    *,
    engine: AsyncEngine,
    household: Household,
    alert_engine: AlertEngine,
    device_service: DevicesService,
    clock: MovableClock,
) -> None:
    clock.at = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    service = energy.build_service(engine=engine, devices=device_service, clock=clock)
    await service.set_tariff(
        household.home,
        timezone="America/Sao_Paulo",
        currency="BRL",
        base_price=Decimal("0.8"),
        periods=(),
        monthly_budget_kwh=Decimal(100),
        expected_version=None,
        actor="owner",
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO energy.hourly (device_id, hour, home_id, wh)"
                " VALUES (:device, :hour, :home, 85000)"
            ),
            {
                "device": household.devices["meter"],
                "hour": datetime(2026, 10, 10, 12, tzinfo=UTC),
                "home": household.home,
            },
        )

    assert await alert_engine.check_budgets() >= 1
    assert await alert_engine.check_budgets() == 0
    mine = [a for a in await alerts_of(engine, household) if a.key.startswith("energy_budget")]
    assert [(a.key, a.status) for a in mine] == [("energy_budget:2026-10:80", "open")]

    clock.at = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)  # a new month in São Paulo
    await alert_engine.check_budgets()
    mine = [a for a in await alerts_of(engine, household) if a.key.startswith("energy_budget")]
    assert [a.status for a in mine] == ["resolved"]


async def test_alerts_flow_from_the_device_event_stream_through_the_consumer_group(
    *,
    engine: AsyncEngine,
    redis: Redis,
    household: Household,
    alert_engine: AlertEngine,
    stream: str,
    clock: MovableClock,
) -> None:
    publisher = RedisEventPublisher(redis, stream=stream, maxlen=1000)
    consumer = StreamConsumer(
        redis, group="notifications", consumer="t", handler=alert_engine.handle_event, stream=stream
    )
    await consumer.ensure_group()
    await publisher.publish(battery(household, 3, clock.now()))
    await consumer.consume_once()
    assert [a.status for a in await alerts_of(engine, household)] == ["open"]


# --- Delivery ----------------------------------------------------------------------------


async def test_queued_emails_reach_the_smtp_server(
    *,
    engine: AsyncEngine,
    household: Household,
    alert_engine: AlertEngine,
    dispatcher: DeliveryDispatcher,
    mailpit: str,
    clock: MovableClock,
) -> None:
    await alert_engine.handle_event("1-0", battery(household, 9, clock.now()))

    assert await dispatcher.run_once() == 2
    assert {d.status for d in await deliveries_of(engine, household)} == {"sent"}
    to = f"{household.owner.principal.user_id}@example.com"
    [mail] = await mailbox(mailpit, to)
    assert mail["Subject"] == "[warning] Sensor battery is low (9%)"


async def test_webhook_deliveries_are_signed_retried_on_5xx_and_carry_an_idempotency_key(
    *,
    api: httpx.AsyncClient,
    engine: AsyncEngine,
    household: Household,
    alert_engine: AlertEngine,
    dispatcher: DeliveryDispatcher,
    receiver: Receiver,
    clock: MovableClock,
) -> None:
    created = await api.put(
        f"/homes/{household.home}/webhook",
        json={"url": receiver.url},
        headers=household.owner.headers,
    )
    assert created.status_code == 200, created.text
    secret = created.json()["secret"]
    receiver.statuses = [503, 204]
    await alert_engine.handle_event("1-0", battery(household, 9, clock.now()))

    await dispatcher.run_once()
    [hook] = [d for d in await deliveries_of(engine, household) if d.channel == "webhook"]
    assert (hook.status, hook.attempts, hook.last_error) == ("queued", 1, "HTTP 503")
    clock.at = hook.next_attempt_at
    await dispatcher.run_once()
    [hook] = [d for d in await deliveries_of(engine, household) if d.channel == "webhook"]
    assert (hook.status, hook.attempts) == ("sent", 2)

    first, second = receiver.requests
    assert first["headers"]["Idempotency-Key"] == second["headers"]["Idempotency-Key"]
    assert second["json"]["type"] == "alert.opened"
    assert second["json"]["alert"]["kind"] == "low_battery"
    assert verify(secret, second["headers"][SIGNATURE_HEADER], second["body"], now=int(time.time()))


async def test_a_webhook_answering_4xx_is_not_retried(
    *,
    api: httpx.AsyncClient,
    engine: AsyncEngine,
    household: Household,
    dispatcher: DeliveryDispatcher,
    receiver: Receiver,
    clock: MovableClock,
) -> None:
    receiver.statuses = [410]
    await api.put(
        f"/homes/{household.home}/webhook",
        json={"url": receiver.url},
        headers=household.owner.headers,
    )
    test = await api.post(f"/homes/{household.home}/webhook/test", headers=household.owner.headers)
    assert test.status_code == 202

    clock.at = datetime.now(UTC)  # the API queued it on the wall clock
    await dispatcher.run_once()
    [hook] = await deliveries_of(engine, household)
    assert (hook.status, hook.last_error) == ("failed", "HTTP 410")
    assert receiver.requests[0]["json"]["type"] == "ping"


async def test_the_dispatchers_of_two_workers_never_send_the_same_delivery_twice(
    *,
    engine: AsyncEngine,
    household: Household,
    alert_engine: AlertEngine,
    dispatcher: DeliveryDispatcher,
    mailpit: str,
    clock: MovableClock,
) -> None:
    for i, device in enumerate(("sensor", "lock")):
        await alert_engine.handle_event(
            f"{i}-0",
            DeviceEvent(
                household.home,
                household.devices[device],
                DeviceEventKind.TELEMETRY,
                clock.now(),
                {"battery_pct": 4.0},
            ),
        )

    attempted = await asyncio.gather(*(dispatcher.run_once() for _ in range(4)))

    assert sum(attempted) == 4  # 2 alerts x 2 members with email
    to = f"{household.resident.principal.user_id}@example.com"
    await eventually(lambda: _count(mailpit, to, 2))


async def _count(mailpit: str, to: str, expected: int) -> bool:
    return len(await mailbox(mailpit, to)) == expected
