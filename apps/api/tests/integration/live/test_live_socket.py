"""The live WebSocket against a real app, Postgres and Redis: snapshot then events,
tenancy and guest scope, cross-site handshakes, revocation, and slow clients."""

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from redis.asyncio import Redis
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from smarthome.main import create_app
from smarthome.modules.devices.api import live
from smarthome.modules.identity.domain.model import Role
from smarthome.shared.config import Settings
from smarthome.shared.events import DeviceEvent, DeviceEventKind
from smarthome.shared.infrastructure.event_stream import (
    DEVICE_EVENTS,
    RedisEventPublisher,
    encode,
)
from tests.integration.automations.conftest import Household
from tests.integration.helpers import Member, sign_in

ORIGIN = "http://localhost:5173"


@pytest.fixture
def live_settings(migrated_database: Settings) -> Settings:
    return Settings.model_validate(
        migrated_database.model_dump() | {"live_recheck_interval_s": 0.2}
    )


@pytest.fixture
async def server(live_settings: Settings) -> AsyncIterator[str]:
    """The app behind a real uvicorn on an ephemeral port; yields its ws:// base URL."""
    config = uvicorn.Config(
        create_app(live_settings), host="127.0.0.1", port=0, log_level="warning", lifespan="on"
    )
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve())
    while not instance.started:  # noqa: ASYNC110 - uvicorn exposes no event
        await asyncio.sleep(0.05)
    port = instance.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}"
    instance.should_exit = True
    await task


def socket(server: str, household: Household, member: Member, origin: str | None = ORIGIN) -> Any:
    extra = {"cookie": member.headers["cookie"]}
    if origin is not None:
        extra["origin"] = origin
    return connect(f"{server}/homes/{household.home}/live", additional_headers=extra, origin=None)


async def receive(ws: ClientConnection) -> dict[str, Any]:
    async with asyncio.timeout(10):
        loaded: dict[str, Any] = json.loads(await ws.recv())
    return loaded


@pytest.fixture
def events(redis: Redis) -> RedisEventPublisher:
    """Publishes where the app's tail reads (the real stream)."""
    return RedisEventPublisher(redis, maxlen=10_000)


def event(household: Household, device: str, kind: DeviceEventKind, **data: Any) -> DeviceEvent:
    return DeviceEvent(household.home, household.devices[device], kind, datetime.now(UTC), data)


async def wait_subscribed() -> None:
    """The app subscribes before sending the snapshot, and the tail reads from `$`; give
    it one block period so an event published now is not before the tail's start."""
    await asyncio.sleep(1.2)


async def test_a_member_gets_a_snapshot_then_live_events_of_their_home_only(
    server: str, household: Household, events: RedisEventPublisher
) -> None:
    async with socket(server, household, household.owner) as ws:
        snapshot = await receive(ws)
        await wait_subscribed()
        other_home = DeviceEvent(
            uuid4(), "light-x", DeviceEventKind.STATE, datetime.now(UTC), {"on": True}
        )
        await events.publish(other_home)
        await events.publish(event(household, "hall", DeviceEventKind.STATE, on=True))
        await events.publish(
            event(household, "climate", DeviceEventKind.TELEMETRY, temperature_c=23.5)
        )
        await events.publish(
            event(
                household,
                "hall",
                DeviceEventKind.COMMAND,
                command_id="c1",
                status="acknowledged",
                reason=None,
            )
        )
        received = [await receive(ws) for _ in range(3)]

    assert snapshot["type"] == "snapshot"
    assert {d["id"] for d in snapshot["devices"]} == set(household.devices.values())
    assert [m["type"] for m in received] == ["state", "telemetry", "command"]
    assert received[0] | {"at": None} == {
        "type": "state",
        "device_id": household.devices["hall"],
        "at": None,
        "on": True,
    }
    assert received[1]["temperature_c"] == 23.5
    assert received[2]["status"] == "acknowledged"


async def test_a_guest_sees_only_the_devices_of_their_pass(
    server: str, household: Household, redis: Redis, events: RedisEventPublisher
) -> None:
    guest = await sign_in(redis, f"guest-{secrets.token_hex(4)}")
    _, token = await household.identity.invite(
        household.owner.principal,
        household.home,
        role=Role.GUEST,
        guest_access_expires_at=datetime.now(UTC) + timedelta(days=1),
        device_scope=frozenset({household.devices["hall"]}),
    )
    await household.identity.accept_invitation(guest.principal, token)

    async with socket(server, household, guest) as ws:
        snapshot = await receive(ws)
        await wait_subscribed()
        await events.publish(event(household, "door", DeviceEventKind.STATE, locked=False))
        await events.publish(event(household, "hall", DeviceEventKind.STATE, on=False))
        first = await receive(ws)

    assert [d["id"] for d in snapshot["devices"]] == [household.devices["hall"]]
    assert first["device_id"] == household.devices["hall"]


@pytest.mark.parametrize("case", ["foreign origin", "no origin", "no session", "not a member"])
async def test_handshakes_that_could_be_hijacked_or_unauthorized_are_refused(
    server: str, household: Household, redis: Redis, case: str
) -> None:
    stranger = await sign_in(redis, f"stranger-{secrets.token_hex(4)}")
    nobody = Member(stranger.principal, {"cookie": "__Host-smarthome_session=forged"})
    member, origin = {
        "foreign origin": (household.owner, "https://evil.example"),
        "no origin": (household.owner, None),
        "no session": (nobody, ORIGIN),
        "not a member": (stranger, ORIGIN),
    }[case]

    with pytest.raises(InvalidStatus) as refused:
        async with socket(server, household, member, origin=origin):
            pass

    assert refused.value.response.status_code == 403


async def test_the_socket_closes_when_the_membership_is_revoked(
    server: str, household: Household, redis: Redis
) -> None:
    resident = await sign_in(redis, f"resident-{secrets.token_hex(4)}")
    _, token = await household.identity.invite(
        household.owner.principal, household.home, role=Role.RESIDENT
    )
    await household.identity.accept_invitation(resident.principal, token)

    async with socket(server, household, resident) as ws:
        await receive(ws)
        await household.identity.revoke_member(
            household.owner.principal, household.home, resident.principal.user_id
        )
        with pytest.raises(ConnectionClosed) as closed:
            await receive(ws)

    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 4403


async def test_a_client_that_cannot_keep_up_is_disconnected_not_buffered_forever(
    server: str, household: Household, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "QUEUE_SIZE", 3)
    async with socket(server, household, household.owner) as ws:
        await receive(ws)
        await wait_subscribed()
        # One pipeline: the tail reads the burst as one batch, faster than any socket.
        async with redis.pipeline(transaction=True) as pipe:
            for i in range(200):
                burst = event(household, "climate", DeviceEventKind.TELEMETRY, temperature_c=i)
                pipe.xadd(DEVICE_EVENTS, encode(burst))  # type: ignore[arg-type]
            await pipe.execute()
        with pytest.raises(ConnectionClosed) as closed:  # noqa: PT012 - drain until closed
            for _ in range(500):
                await receive(ws)

    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 1013
