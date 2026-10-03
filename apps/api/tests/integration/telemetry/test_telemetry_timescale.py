"""Telemetry against real TimescaleDB (hypertable, continuous aggregates) and the broker."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import aiomqtt
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from device_protocol import DeviceKind, MessageKind, topic
from smarthome.modules.devices import wiring as devices
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.devices.domain.model import DeviceStatus
from smarthome.modules.telemetry import wiring as telemetry
from smarthome.modules.telemetry.api.mqtt import handlers as telemetry_handlers
from smarthome.modules.telemetry.domain.model import Reading, Resolution
from smarthome.modules.telemetry.infrastructure.timescale import TimescaleReadings
from smarthome.shared.config import Settings
from smarthome.shared.infrastructure.mqtt_consumer import (
    ConsumerConfig,
    DeviceTrafficConsumer,
    subscription_acls,
)
from tests.integration.devices.test_devices_e2e import eventually, new_code
from tests.integration.mqtt_broker import Broker

NOW = datetime.now(UTC).replace(microsecond=0)


def readings(
    device: str, start: datetime, count: int, step: timedelta, value: float = 10.0
) -> list[Reading]:
    home = uuid4()
    return [
        Reading(start + i * step, home, device, "power_w", value + i, f"msg-{i:08d}", NOW)
        for i in range(count)
    ]


async def refresh_aggregates(engine: AsyncEngine, start: datetime, end: datetime) -> None:
    from sqlalchemy import text  # noqa: PLC0415

    async with engine.connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for view in ("telemetry.readings_1m", "telemetry.readings_1h"):
            await autocommit.execute(
                text(
                    f"CALL refresh_continuous_aggregate('{view}',"
                    " CAST(:start AS timestamptz), CAST(:end AS timestamptz))"
                ),
                {"start": start, "end": end},
            )


async def test_writing_the_same_batch_twice_stores_it_once(engine: AsyncEngine) -> None:
    store = TimescaleReadings(engine)
    batch = readings(f"dev-{uuid4().hex[:8]}", NOW - timedelta(minutes=5), 50, timedelta(seconds=1))

    assert await store.write(batch) == 50
    assert await store.write(batch) == 0


async def test_history_comes_from_raw_rows_or_continuous_aggregates_with_the_same_totals(
    engine: AsyncEngine,
) -> None:
    store = TimescaleReadings(engine)
    device = f"dev-{uuid4().hex[:8]}"
    start = (NOW - timedelta(hours=3)).replace(second=0)
    batch = readings(device, start, 180, timedelta(minutes=1), value=0.0)  # values 0..179
    home = batch[0].home_id
    await store.write(batch)
    # Backfilled rows land in a range the refresh policy may already have materialized
    # (it runs whenever earlier tests left data behind); they show up at its next run.
    # Refresh now instead of depending on when the policy last ran.
    await refresh_aggregates(engine, start - timedelta(hours=1), NOW + timedelta(hours=1))

    raw = await store.series(
        home_id=home,
        device_id=device,
        metric="power_w",
        start=start,
        end=NOW,
        resolution=Resolution.RAW,
    )
    minutes = await store.series(
        home_id=home,
        device_id=device,
        metric="power_w",
        start=start,
        end=NOW,
        resolution=Resolution.MINUTE,
    )
    hours = await store.series(
        home_id=home,
        device_id=device,
        metric="power_w",
        start=start - timedelta(hours=1),
        end=NOW,
        resolution=Resolution.HOUR,
    )

    assert len(raw) == len(minutes) == 180
    assert sum(p.samples for p in hours) == 180
    assert sum(p.avg * p.samples for p in hours) == pytest.approx(sum(range(180)))
    assert min(p.min for p in hours) == 0
    assert max(p.max for p in hours) == 179


async def test_retention_follows_configuration_and_can_be_reapplied(
    engine: AsyncEngine, device_settings: Settings
) -> None:
    from sqlalchemy import text  # noqa: PLC0415

    custom = Settings.model_validate(
        device_settings.model_dump() | {"telemetry_raw_retention_days": 14}
    )
    await telemetry.apply_configured_retention(custom, engine)
    await telemetry.apply_configured_retention(custom, engine)  # idempotent

    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT hypertable_name, config->>'drop_after' FROM timescaledb_information.jobs"
                " WHERE proc_name = 'policy_retention' ORDER BY 1"
            )
        )
        policies = {str(r[0]): str(r[1]) for r in rows}
    assert policies["readings"] == "14 days"
    assert policies["readings_1m"] == "90 days"


@pytest.fixture
async def running_ingestor(
    device_settings: Settings, engine: AsyncEngine, redis: Redis, service: DevicesService
) -> AsyncIterator[tuple[DeviceTrafficConsumer, asyncio.Event]]:
    flood_limit = Settings.model_validate(
        device_settings.model_dump()
        | {"telemetry_max_messages_per_minute": 20, "telemetry_flush_interval_ms": 100}
    )
    await devices.broker_admin(flood_limit).ensure_service_account(
        username=flood_limit.mqtt_hub_user,
        password=flood_limit.mqtt_hub_password.get_secret_value(),
        acls=subscription_acls(),
    )
    ingest, buffer = telemetry.build_ingest(
        flood_limit, engine=engine, redis=redis, devices=service
    )
    consumer = DeviceTrafficConsumer(
        ConsumerConfig(
            host=flood_limit.mqtt_host,
            port=flood_limit.mqtt_port,
            ca_file=flood_limit.mqtt_ca_file,
            username=flood_limit.mqtt_hub_user,
            password=flood_limit.mqtt_hub_password.get_secret_value(),
        ),
        telemetry_handlers(ingest),
    )
    stop = asyncio.Event()
    tasks = [asyncio.create_task(consumer.run(stop)), asyncio.create_task(buffer.run(stop))]
    await asyncio.sleep(0.5)
    yield consumer, stop
    stop.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


def telemetry_payload(i: int, **values: float) -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "message_id": f"m-{i:010d}",
            "seq": i,
            "ts": (NOW + timedelta(milliseconds=i)).isoformat(),
            "readings": values or {"power_w": 5.0 + i},
        }
    ).encode()


async def test_device_telemetry_reaches_the_hypertable_once_with_latest_values_in_redis(
    running_ingestor: tuple[DeviceTrafficConsumer, asyncio.Event],
    service: DevicesService,
    engine: AsyncEngine,
    redis: Redis,
    broker: Broker,
) -> None:
    from sqlalchemy import text  # noqa: PLC0415

    home = uuid4()
    claimed = await service.claim(
        code=await new_code(service, home), kind=DeviceKind.PLUG, firmware=None
    )
    device_id = claimed.device.id
    name = topic(str(home), device_id, MessageKind.TELEMETRY)

    async with broker.connect(device_id, claimed.mqtt_password) as device:
        for i in range(5):
            await device.publish(name, telemetry_payload(i), qos=1)
        await device.publish(name, telemetry_payload(4), qos=1)  # a redelivery
        await device.publish(name, b'{"schema_version":"1","readings":{}}', qos=1)  # invalid

    async def stored() -> int:
        async with engine.connect() as conn:
            return int(
                await conn.scalar(
                    text("SELECT count(*) FROM telemetry.readings WHERE device_id = :d"),
                    {"d": device_id},
                )
                or 0
            )

    await eventually(lambda: _equals(stored, 5))
    latest = json.loads(await redis.hget(f"device:{device_id}:readings", "power_w") or "{}")
    assert latest["value"] == 9.0


async def _equals(fn: object, expected: int) -> bool:
    return await fn() == expected  # type: ignore[operator, no-any-return]


async def test_a_device_flooding_telemetry_is_quarantined_and_cut_off(
    running_ingestor: tuple[DeviceTrafficConsumer, asyncio.Event],
    service: DevicesService,
    broker: Broker,
) -> None:
    home = uuid4()
    claimed = await service.claim(
        code=await new_code(service, home), kind=DeviceKind.PLUG, firmware=None
    )
    device_id = claimed.device.id
    name = topic(str(home), device_id, MessageKind.TELEMETRY)

    async def flood() -> None:
        async with broker.connect(device_id, claimed.mqtt_password) as device:
            for i in range(200):
                await device.publish(name, telemetry_payload(1000 + i), qos=1)
                await asyncio.sleep(0.005)
            await asyncio.wait_for(_drain(device), timeout=5)

    with pytest.raises(aiomqtt.MqttError):  # kicked by the broker mid-flood
        await flood()

    # The broker cuts the device off first; the status change is recorded right after.
    async def quarantined() -> bool:
        return (await service.get(home, device_id)).device.status is DeviceStatus.QUARANTINED

    await eventually(quarantined)
    view = await service.get(home, device_id)
    assert view.device.status_reason == "telemetry rate limit exceeded"


async def _drain(client: aiomqtt.Client) -> None:
    async for _ in client.messages:
        pass
