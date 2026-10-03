"""A command from an HTTP request to the device and back, over real Postgres, Redis and
Mosquitto, with the worker's relay and the ingestor's consumer running."""

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import aiomqtt
import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from device_protocol import DeviceKind, MessageKind, encode, topic
from smarthome.modules.commands import wiring as commands
from smarthome.modules.commands.api.mqtt import handlers as command_handlers
from smarthome.modules.devices import wiring as devices
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.identity.application.services import IdentityService
from smarthome.modules.identity.domain.model import HomeId, Role
from smarthome.modules.identity.infrastructure.persistence import PostgresUnitOfWork
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings
from smarthome.shared.infrastructure.mqtt_consumer import (
    ConsumerConfig,
    DeviceTrafficConsumer,
    subscription_acls,
    user_properties,
)
from smarthome.shared.infrastructure.mqtt_publisher import MqttPublisher, PublisherConfig
from smarthome.shared.infrastructure.outbox import OutboxRelay
from tests.integration.helpers import Member, eventually, sign_in
from tests.integration.mqtt_broker import Broker


@dataclass(frozen=True)
class Household:
    home: HomeId
    owner: Member
    light: str
    light_password: str
    lock: str


@pytest.fixture
async def household(engine: AsyncEngine, redis: Redis, service: DevicesService) -> Household:
    identity = IdentityService(lambda: PostgresUnitOfWork(engine), SystemClock())
    owner = await sign_in(redis, f"owner-{secrets.token_hex(4)}")
    home = (await identity.create_home(owner.principal, name="Casa", timezone="UTC")).id

    async def pair(kind: DeviceKind) -> tuple[str, str]:
        _, code = await service.create_pairing_code(home_id=home, actor=owner.principal.user_id)
        claimed = await service.claim(code=code, kind=kind, firmware=None)
        return claimed.device.id, claimed.mqtt_password

    light, light_password = await pair(DeviceKind.LIGHT)
    lock, _ = await pair(DeviceKind.LOCK)
    return Household(home, owner, light, light_password, lock)


@pytest.fixture
async def hub(
    device_settings: Settings, engine: AsyncEngine, service: DevicesService
) -> AsyncIterator[None]:
    """The worker's relay and the ingestor's ack consumer, as their entrypoints run them."""
    settings = device_settings
    await devices.broker_admin(settings).ensure_service_account(
        username=settings.mqtt_hub_user,
        password=settings.mqtt_hub_password.get_secret_value(),
        acls=subscription_acls(),
    )
    password = settings.mqtt_hub_password.get_secret_value()
    publisher = MqttPublisher(
        PublisherConfig(
            settings.mqtt_host,
            settings.mqtt_port,
            settings.mqtt_ca_file,
            settings.mqtt_hub_user,
            password,
        )
    )
    relay = OutboxRelay(engine, publisher, SystemClock(), poll_interval_s=0.2)
    acks = commands.build_service(settings, engine=engine, devices=service, clock=SystemClock())
    consumer = DeviceTrafficConsumer(
        ConsumerConfig(
            settings.mqtt_host,
            settings.mqtt_port,
            settings.mqtt_ca_file,
            settings.mqtt_hub_user,
            password,
        ),
        command_handlers(acks),
    )
    stop = asyncio.Event()
    tasks = [asyncio.create_task(relay.run(stop)), asyncio.create_task(consumer.run(stop))]
    await asyncio.sleep(0.5)  # subscribed
    yield
    stop.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    await publisher.close()


def ack_properties(received: aiomqtt.Message) -> Properties:
    """What a device without its own tracer does: hand the trace context straight back."""
    properties = Properties(PacketTypes.PUBLISH)  # type: ignore[no-untyped-call]
    properties.UserProperty = list(user_properties(received).items())
    return properties


async def test_a_command_travels_to_the_device_and_its_outcome_comes_back_in_one_trace(
    hub: None,
    household: Household,
    api: httpx.AsyncClient,
    broker: Broker,
    span_exporter: InMemorySpanExporter,
) -> None:
    home, light = household.home, household.light
    span_exporter.clear()
    async with broker.connect(light, household.light_password) as device:
        await device.subscribe(topic(str(home), light, MessageKind.COMMAND), qos=1)

        response = await api.post(
            f"/homes/{home}/devices/{light}/commands",
            json={"action": "set_state", "desired": {"on": True, "brightness_pct": 60}},
            headers=household.owner.headers,
        )
        assert response.status_code == 202, response.text
        sent = response.json()
        assert sent["status"] == "pending"
        assert response.headers["location"].endswith(sent["id"])

        async with asyncio.timeout(10):
            received = await anext(aiter(device.messages))
        assert isinstance(received.payload, bytes)
        command = json.loads(received.payload)
        assert command["command_id"] == sent["id"]
        assert command["desired"] == {"on": True, "brightness_pct": 60}
        assert "traceparent" in user_properties(received)

        ack_topic = topic(str(home), light, MessageKind.COMMAND_ACK)
        for status in ("received", "applied"):
            ack = {
                "schema_version": "1",
                "command_id": sent["id"],
                "ts": "2026-10-02T12:00:00Z",
                "status": status,
            }
            await device.publish(
                ack_topic,
                encode(MessageKind.COMMAND_ACK, ack),
                qos=1,
                properties=ack_properties(received),
            )

    url = f"/homes/{home}/devices/{light}/commands/{sent['id']}"

    async def acknowledged() -> bool:
        body = (await api.get(url, headers=household.owner.headers)).json()
        return bool(body["status"] == "acknowledged")

    await eventually(acknowledged)
    final = (await api.get(url, headers=household.owner.headers)).json()
    assert final["delivered_at"] is not None
    assert final["completed_at"] is not None
    twin = (await api.get(f"/homes/{home}/devices/{light}", headers=household.owner.headers)).json()
    assert twin["twin"]["desired"] == {"on": True, "brightness_pct": 60}

    # One trace: the HTTP request, the relay's publish and the ingestion of both acks.
    trace_id = int(final["trace_id"], 16)
    names = [s.name for s in span_exporter.get_finished_spans() if s.context.trace_id == trace_id]
    assert "outbox publish" in names
    assert names.count("ingest device message") == 2


async def test_a_lock_needs_a_recent_sign_in_and_the_client_is_told_where_to_get_one(
    household: Household, api: httpx.AsyncClient, redis: Redis
) -> None:
    url = f"/homes/{household.home}/devices/{household.lock}/commands"
    stale = await sign_in(
        redis, household.owner.principal.user_id, authenticated_ago=timedelta(minutes=30)
    )

    refused = await api.post(
        url, json={"action": "set_state", "desired": {"locked": False}}, headers=stale.headers
    )
    fresh = await api.post(
        url,
        json={"action": "set_state", "desired": {"locked": False}},
        headers=household.owner.headers,
    )

    assert refused.status_code == 401
    problem = refused.json()
    assert problem["type"] == "urn:smarthome:problem:reauthentication-required"
    assert problem["login_url"].endswith("/auth/login?reauth=true")
    assert fresh.status_code == 202


async def invite(
    engine: AsyncEngine,
    redis: Redis,
    household: Household,
    role: Role,
    scope: frozenset[str] | None = None,
) -> Member:
    identity = IdentityService(lambda: PostgresUnitOfWork(engine), SystemClock())
    _, token = await identity.invite(
        household.owner.principal,
        household.home,
        role=role,
        guest_access_expires_at=(
            SystemClock().now() + timedelta(days=1) if role is Role.GUEST else None
        ),
        device_scope=scope,
    )
    member = await sign_in(redis, f"{role.value}-{secrets.token_hex(4)}")
    await identity.accept_invitation(member.principal, token)
    return member


@pytest.mark.parametrize(
    ("role", "scope", "target", "body", "expected"),
    [
        (Role.VIEWER, None, "light", {"action": "identify"}, 403),
        (Role.GUEST, frozenset({"light"}), "light", {"action": "identify"}, 202),
        (Role.GUEST, frozenset({"light"}), "lock", {"action": "identify"}, 403),
        (Role.GUEST, frozenset({"light"}), "light", {"action": "reboot"}, 403),
        (Role.RESIDENT, None, "light", {"action": "reboot"}, 202),
    ],
)
async def test_who_may_send_what(  # one row of the permission matrix
    *,
    household: Household,
    api: httpx.AsyncClient,
    engine: AsyncEngine,
    redis: Redis,
    role: Role,
    scope: frozenset[str] | None,
    target: str,
    body: dict[str, Any],
    expected: int,
) -> None:
    devices_by_name = {"light": household.light, "lock": household.lock}
    scope_ids = frozenset(devices_by_name[n] for n in scope) if scope else None
    member = await invite(engine, redis, household, role, scope_ids)

    response = await api.post(
        f"/homes/{household.home}/devices/{devices_by_name[target]}/commands",
        json=body,
        headers=member.headers,
    )

    assert response.status_code == expected, response.text


async def test_a_quarantined_device_or_an_impossible_command_is_refused_before_queueing(
    household: Household, api: httpx.AsyncClient, service: DevicesService
) -> None:
    headers = household.owner.headers
    url = f"/homes/{household.home}/devices/{household.light}/commands"

    impossible = await api.post(
        url, json={"action": "set_state", "desired": {"locked": True}}, headers=headers
    )
    await service.quarantine(household.home, household.light, actor="test", reason="testing")
    quarantined = await api.post(url, json={"action": "identify"}, headers=headers)

    assert impossible.status_code == 422
    assert quarantined.status_code == 409
    assert (await api.get(url, headers=headers)).json() == []
