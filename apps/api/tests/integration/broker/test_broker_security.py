"""The broker enforces the device security model: TLS only, one identity per device,
ACLs scoped to the device's own topics, immediate revocation, and Last Will."""

import asyncio
import contextlib
import gc
import json
import secrets
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiomqtt
import pytest
from aiomqtt import Will
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from device_protocol import MessageKind, encode, hub_subscription, topic
from tests.integration.mqtt_broker import Broker

DELIVERY_WAIT = 1.5


@dataclass(frozen=True)
class Device:
    home_id: str
    device_id: str
    password: str

    def topic(self, kind: MessageKind) -> str:
        return topic(self.home_id, self.device_id, kind)


@pytest.fixture
async def device(broker: Broker) -> Device:
    created = Device("home-a", f"dev-{secrets.token_hex(4)}", secrets.token_urlsafe(24))
    await broker.admin().register_device(
        home_id=created.home_id, device_id=created.device_id, password=created.password
    )
    return created


@pytest.fixture
async def other_device(broker: Broker) -> Device:
    created = Device("home-b", f"dev-{secrets.token_hex(4)}", secrets.token_urlsafe(24))
    await broker.admin().register_device(
        home_id=created.home_id, device_id=created.device_id, password=created.password
    )
    return created


@asynccontextmanager
async def hub_observer(broker: Broker) -> AsyncIterator[aiomqtt.Client]:
    username, password = f"observer-{secrets.token_hex(4)}", secrets.token_urlsafe(16)
    await broker.admin().create_service_account(
        username=username,
        password=password,
        acls=[{"acltype": "subscribePattern", "topic": "v1/#", "allow": True}],
    )
    async with broker.connect(username, password) as client:
        for kind in MessageKind:
            await client.subscribe(hub_subscription(kind), qos=1)
        yield client


async def received(client: aiomqtt.Client, wait: float = DELIVERY_WAIT) -> list[aiomqtt.Message]:
    """Everything delivered to `client` within `wait` seconds."""
    messages: list[aiomqtt.Message] = []

    async def collect() -> None:
        # Appended one by one: the timeout cancels this coroutine and must keep what arrived.
        async for message in client.messages:
            messages.append(message)  # noqa: PERF401

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(collect(), timeout=wait)
    return messages


def refused(code: int | ReasonCode) -> bool:
    return isinstance(code, ReasonCode) and code.is_failure


async def drain(client: aiomqtt.Client) -> None:
    async for _ in client.messages:
        pass


# paho leaves the TLS socket open when the handshake fails; that leak is the library's.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
async def test_a_client_that_does_not_trust_the_broker_certificate_cannot_connect(
    broker: Broker, device: Device
) -> None:
    untrusting = ssl.create_default_context()  # system CAs only, not the dev CA

    with pytest.raises(aiomqtt.MqttError):
        async with aiomqtt.Client(
            broker.host,
            broker.port,
            username=device.device_id,
            password=device.password,
            tls_context=untrusting,
            timeout=3,
        ):
            pass
    # Finalize the leaked socket here, where the warning filter applies, rather than
    # during some later test's garbage collection.
    gc.collect()


async def test_a_wrong_password_is_refused(broker: Broker, device: Device) -> None:
    with pytest.raises(aiomqtt.MqttError, match=r"135|Not authorized|Bad user name or password"):
        async with broker.connect(device.device_id, "not-the-password"):
            pass


async def test_a_device_cannot_borrow_another_devices_client_id(
    broker: Broker, device: Device, other_device: Device
) -> None:
    with pytest.raises(aiomqtt.MqttError):
        async with broker.connect(
            device.device_id, device.password, client_id=other_device.device_id
        ):
            pass


async def test_a_device_publishes_on_its_own_topics_and_the_hub_receives_them(
    broker: Broker, device: Device
) -> None:
    async with (
        hub_observer(broker) as hub,
        broker.connect(device.device_id, device.password) as dev,
    ):
        payload = encode(
            MessageKind.PRESENCE, {"schema_version": "1", "status": "online", "reason": "boot"}
        )
        await dev.publish(device.topic(MessageKind.PRESENCE), payload, qos=1)

        messages = await received(hub)

    assert [str(m.topic) for m in messages] == [device.topic(MessageKind.PRESENCE)]


async def test_a_device_cannot_publish_as_another_device(
    broker: Broker, device: Device, other_device: Device
) -> None:
    async with (
        hub_observer(broker) as hub,
        broker.connect(device.device_id, device.password) as dev,
    ):
        await dev.publish(other_device.topic(MessageKind.TELEMETRY), b"{}", qos=1)
        await dev.publish(other_device.topic(MessageKind.COMMAND_ACK), b"{}", qos=1)

        messages = await received(hub)

    assert messages == []


@pytest.mark.parametrize(
    "pattern",
    ["v1/#", "v1/homes/+/devices/+/commands", "$SYS/#", "{other}", "{own_telemetry}"],
)
async def test_a_device_may_only_subscribe_to_its_own_command_topic(
    broker: Broker, device: Device, other_device: Device, pattern: str
) -> None:
    topic_filter = pattern.format(
        other=other_device.topic(MessageKind.COMMAND),
        own_telemetry=device.topic(MessageKind.TELEMETRY),
    )
    async with broker.connect(device.device_id, device.password) as dev:
        own = await dev.subscribe(device.topic(MessageKind.COMMAND), qos=1)
        foreign = await dev.subscribe(topic_filter, qos=1)

    assert not refused(own[0])
    assert refused(foreign[0])


async def test_a_device_that_vanishes_is_reported_offline_by_its_last_will(
    broker: Broker, device: Device
) -> None:
    offline = encode(
        MessageKind.PRESENCE,
        {"schema_version": "1", "status": "offline", "reason": "connection_lost"},
    )
    will = Will(device.topic(MessageKind.PRESENCE), offline, qos=1, retain=True)
    async with hub_observer(broker) as hub:
        async with broker.connect(device.device_id, device.password, will=will) as dev:
            # MQTT 5 "disconnect with Will message": what a power cut looks like to the broker.
            dev._client.disconnect(reasoncode=ReasonCode(PacketTypes.DISCONNECT, identifier=0x04))
            await asyncio.sleep(0.2)

        messages = await received(hub)

    assert [json.loads(bytes(m.payload))["status"] for m in messages] == ["offline"]
    assert messages[0].retain is False  # live delivery; the retained copy serves late subscribers


async def test_revoking_a_device_disconnects_it_immediately_and_locks_it_out(
    broker: Broker, device: Device
) -> None:
    async with broker.connect(device.device_id, device.password) as dev:
        await dev.subscribe(device.topic(MessageKind.COMMAND), qos=1)
        await broker.admin().remove_device(device.device_id)

        with pytest.raises(aiomqtt.MqttError):
            await asyncio.wait_for(drain(dev), timeout=3)

    with pytest.raises(aiomqtt.MqttError):
        async with broker.connect(device.device_id, device.password):
            pass


async def test_a_quarantined_device_is_kicked_and_can_come_back_once_released(
    broker: Broker, device: Device
) -> None:
    admin = broker.admin()
    await admin.disable_device(device.device_id)
    with pytest.raises(aiomqtt.MqttError):
        async with broker.connect(device.device_id, device.password):
            pass

    await admin.enable_device(device.device_id)
    async with broker.connect(device.device_id, device.password):
        pass
