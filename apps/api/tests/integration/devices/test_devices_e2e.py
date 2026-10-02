"""Pairing, lifecycle and ingestion against real Postgres, Redis and Mosquitto."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import aiomqtt
import httpx
import pytest
from aiomqtt import Will
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from device_protocol import DeviceKind, MessageKind, encode, topic
from smarthome.modules.devices import wiring as devices
from smarthome.modules.devices.api.mqtt import handlers
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.devices.domain.errors import InvalidPairingCode
from smarthome.modules.devices.infrastructure.broker_admin import BrokerAdmin
from smarthome.modules.devices.infrastructure.live_state import RedisLiveState
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings
from smarthome.shared.infrastructure.mqtt_consumer import (
    ConsumerConfig,
    DeviceTrafficConsumer,
    subscription_acls,
)
from tests.integration.conftest import client_for
from tests.integration.mqtt_broker import Broker

TS = "2026-10-02T12:00:00Z"


async def eventually(check: Callable[[], Awaitable[bool]], within_s: float = 5.0) -> None:
    """Poll until `check` holds: the effect crosses a broker and a consumer task."""
    async with asyncio.timeout(within_s):
        while not await check():  # noqa: ASYNC110 - polling state owned by another process
            await asyncio.sleep(0.1)


async def new_code(service: DevicesService, home: UUID, **kw: Any) -> str:
    return (await service.create_pairing_code(home_id=home, actor="alice", **kw))[1]


class RecordingBroker(BrokerAdmin):
    def __init__(self, inner: BrokerAdmin) -> None:
        super().__init__(inner._conn)
        self.registered: dict[str, str] = {}

    async def register_device(self, *, home_id: str, device_id: str, password: str) -> None:
        self.registered[device_id] = password
        await super().register_device(home_id=home_id, device_id=device_id, password=password)


async def test_a_device_pairs_over_http_and_its_credentials_work_at_the_broker(
    service: DevicesService, api: httpx.AsyncClient, broker: Broker
) -> None:
    home = uuid4()
    code = await new_code(service, home, name="Porch light", room="entrance")

    response = await api.post(
        "/provisioning/claim", json={"code": code.lower(), "kind": "light", "firmware": "2.1.0"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["home_id"] == str(home)
    assert body["topics"]["presence"] == topic(str(home), body["device_id"], MessageKind.PRESENCE)
    async with broker.connect(body["mqtt"]["username"], body["mqtt"]["password"]) as device:
        granted = await device.subscribe(body["topics"]["command"], qos=1)
    assert not getattr(granted[0], "is_failure", True)


async def test_two_devices_racing_on_one_code_get_one_identity_and_the_loser_cannot_log_in(
    device_settings: Settings, engine: AsyncEngine, redis: Redis, broker: Broker
) -> None:
    recording = RecordingBroker(devices.broker_admin(device_settings))
    service = DevicesService(
        devices.build_service(
            device_settings, engine=engine, redis=redis, clock=SystemClock()
        )._uow,
        recording,
        RedisLiveState(redis),
        SystemClock(),
    )
    code = await new_code(service, uuid4())

    results = await asyncio.gather(
        service.claim(code=code, kind=DeviceKind.PLUG, firmware=None),
        service.claim(code=code, kind=DeviceKind.PLUG, firmware=None),
        return_exceptions=True,
    )

    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1
    assert sum(isinstance(r, InvalidPairingCode) for r in results) == 1
    (loser,) = set(recording.registered) - {winners[0].device.id}
    with pytest.raises(aiomqtt.MqttError):
        async with broker.connect(loser, recording.registered[loser]):
            pass


async def test_pairing_attempts_are_rate_limited_per_client(
    device_settings: Settings, redis: Redis
) -> None:
    # Every in-process request comes from 127.0.0.1: start from an empty window.
    for key in [k async for k in redis.scan_iter("ratelimit:claim-*")]:
        await redis.delete(key)
    strict = Settings.model_validate(device_settings.model_dump() | {"claim_rate_per_minute": 3})
    async for api in client_for(strict):
        statuses = [
            (
                await api.post("/provisioning/claim", json={"code": "AAAA-AAAA", "kind": "plug"})
            ).status_code
            for _ in range(5)
        ]

    assert statuses[:3] == [410, 410, 410]
    assert statuses[3:] == [429, 429]


async def test_revoking_a_device_disconnects_it_at_once(
    service: DevicesService, broker: Broker
) -> None:
    home = uuid4()
    claimed = await service.claim(
        code=await new_code(service, home), kind=DeviceKind.LOCK, firmware=None
    )

    async with broker.connect(claimed.device.id, claimed.mqtt_password) as device:
        await service.revoke(home, claimed.device.id, actor="alice", reason="lost")
        with pytest.raises(aiomqtt.MqttError):
            await asyncio.wait_for(_drain(device), timeout=3)


async def _drain(client: aiomqtt.Client) -> None:
    async for _ in client.messages:
        pass


@pytest.fixture
async def ingestor(device_settings: Settings, service: DevicesService) -> Any:
    admin = devices.broker_admin(device_settings)
    # Twice: the second call must reconcile an existing account, not fail.
    for _ in range(2):
        await admin.ensure_service_account(
            username=device_settings.mqtt_hub_user,
            password=device_settings.mqtt_hub_password.get_secret_value(),
            acls=subscription_acls(),
        )
    consumer = DeviceTrafficConsumer(
        ConsumerConfig(
            host=device_settings.mqtt_host,
            port=device_settings.mqtt_port,
            ca_file=device_settings.mqtt_ca_file,
            username=device_settings.mqtt_hub_user,
            password=device_settings.mqtt_hub_password.get_secret_value(),
        ),
        handlers(service),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    await asyncio.sleep(0.5)
    assert not task.done(), task.exception()
    yield consumer
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_a_device_clock_running_ahead_cannot_hide_its_last_will(
    ingestor: DeviceTrafficConsumer, service: DevicesService, broker: Broker
) -> None:
    home = uuid4()
    claimed = await service.claim(
        code=await new_code(service, home), kind=DeviceKind.PLUG, firmware=None
    )
    device_id = claimed.device.id
    presence = topic(str(home), device_id, MessageKind.PRESENCE)
    offline = encode(
        MessageKind.PRESENCE,
        {"schema_version": "1", "status": "offline", "reason": "connection_lost"},
    )
    a_day_ahead = "2099-01-01T00:00:00Z"

    async with broker.connect(
        device_id, claimed.mqtt_password, will=Will(presence, offline, qos=1)
    ) as device:
        online = {"schema_version": "1", "status": "online", "reason": "boot", "ts": a_day_ahead}
        await device.publish(presence, encode(MessageKind.PRESENCE, online), qos=1)
        await eventually(lambda: _is_online(service, home, device_id, True))
        device._client.disconnect(reasoncode=ReasonCode(PacketTypes.DISCONNECT, identifier=0x04))
        await asyncio.sleep(0.2)

    await eventually(lambda: _is_online(service, home, device_id, False))


async def _is_online(service: DevicesService, home: UUID, device_id: str, expected: bool) -> bool:
    return (await service.get(home, device_id)).device.online is expected


async def test_presence_last_will_and_reported_state_flow_from_the_broker_into_the_registry(
    ingestor: DeviceTrafficConsumer, service: DevicesService, redis: Redis, broker: Broker
) -> None:
    home = uuid4()
    claimed = await service.claim(
        code=await new_code(service, home), kind=DeviceKind.LIGHT, firmware=None
    )
    device_id = claimed.device.id
    offline = encode(
        MessageKind.PRESENCE,
        {"schema_version": "1", "status": "offline", "reason": "connection_lost"},
    )
    will = Will(topic(str(home), device_id, MessageKind.PRESENCE), offline, qos=1, retain=True)
    live = RedisLiveState(redis)

    async with broker.connect(device_id, claimed.mqtt_password, will=will) as device:
        await device.publish(
            topic(str(home), device_id, MessageKind.PRESENCE),
            encode(
                MessageKind.PRESENCE,
                {"schema_version": "1", "status": "online", "reason": "boot", "ts": TS},
            ),
            qos=1,
            retain=True,
        )
        await device.publish(
            topic(str(home), device_id, MessageKind.STATE),
            encode(
                MessageKind.STATE,
                {
                    "schema_version": "1",
                    "message_id": "abcdefgh1",
                    "ts": TS,
                    "reported": {"on": True},
                },
            ),
            qos=1,
            retain=True,
        )

        async def online() -> bool:
            view = await service.get(home, device_id)
            return view.device.online and view.twin.reported == {"on": True}

        await eventually(online)
        assert (await live.get(device_id)) == {"online": True, "reported": {"on": True}}

        device._client.disconnect(reasoncode=ReasonCode(PacketTypes.DISCONNECT, identifier=0x04))
        await asyncio.sleep(0.2)

    async def offline_now() -> bool:
        return not (await service.get(home, device_id)).device.online

    await eventually(offline_now)
    assert (await live.get(device_id))["online"] is False


@pytest.mark.parametrize(
    ("topic_name", "raw", "expected"),
    [
        (
            "v1/homes/not-a-uuid/devices/x/presence",
            b'{"schema_version":"1","status":"online"}',
            "invalid",
        ),
        (
            f"v1/homes/{uuid4()}/devices/ghost/presence",
            b'{"schema_version":"1","status":"online"}',
            "ignored",
        ),
        (f"v1/homes/{uuid4()}/devices/x/state", b"{garbage", "invalid"),
        (f"v1/homes/{uuid4()}/devices/x/telemetry", b"{}", "ignored"),
        ("some/other/topic", b"{}", "invalid"),
    ],
)
async def test_the_ingestor_drops_what_it_cannot_trust(
    service: DevicesService, topic_name: str, raw: bytes, expected: str
) -> None:
    consumer = DeviceTrafficConsumer(
        ConsumerConfig(host="unused", port=0, ca_file="", username="", password=""),
        handlers(service),
    )

    assert await consumer.handle(topic_name, raw) == expected


def test_secret_settings_are_not_echoed(device_settings: Settings) -> None:
    assert isinstance(device_settings.mqtt_hub_password, SecretStr)
    assert "integration-hub" not in repr(device_settings)
