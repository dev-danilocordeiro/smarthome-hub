"""Live view of a home over a WebSocket (ADR 0011).

    GET /homes/{home_id}/live   (WebSocket)

On connect the server sends a `snapshot` (every device the member may see, with presence,
reported state and latest readings, all from the Redis read model), then one message per
device event: `state`, `presence`, `telemetry`, `command`. The client never sends
anything that matters; it reconnects (and gets a new snapshot) if the socket drops.

Each API process tails the device event stream once and fans events out to its sockets
(`LiveHub`). A socket that cannot keep up is closed rather than buffered without bound:
the client reconnects and starts again from a snapshot.
"""

import asyncio
import contextlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from opentelemetry import metrics

from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.identity.public import (
    HomeAccess,
    Permission,
    recheck_websocket_access,
    require_websocket_home_access,
)
from smarthome.modules.telemetry.public import TelemetryQueries
from smarthome.shared.events import DeviceEvent

log = structlog.get_logger(__name__)
meter = metrics.get_meter(__name__)

sockets_gauge = meter.create_up_down_counter(
    "smarthome.live.sockets", unit="{socket}", description="Open live WebSockets."
)
dropped_counter = meter.create_counter(
    "smarthome.live.dropped",
    unit="{socket}",
    description="Live sockets closed because the client could not keep up.",
)

router = APIRouter(tags=["live"])

QUEUE_SIZE = 256
CLOSE_TRY_AGAIN_LATER = 1013


def message(event: DeviceEvent) -> dict[str, Any]:
    return {
        "type": event.kind.value,
        "device_id": event.device_id,
        "at": event.at.isoformat(),
        **event.data,
    }


@dataclass(eq=False)
class Subscriber:
    scope: frozenset[str] | None
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE)
    )
    overflowed: asyncio.Event = field(default_factory=asyncio.Event)

    def offer(self, event: DeviceEvent) -> None:
        if self.scope is not None and event.device_id not in self.scope:
            return
        try:
            self.queue.put_nowait(message(event))
        except asyncio.QueueFull:
            self.overflowed.set()


class LiveHub:
    """In-process fan-out from the event stream to the sockets of each home."""

    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[Subscriber]] = defaultdict(set)

    def subscribe(self, home_id: UUID, *, scope: frozenset[str] | None) -> Subscriber:
        subscriber = Subscriber(scope)
        self._subscribers[home_id].add(subscriber)
        return subscriber

    def unsubscribe(self, home_id: UUID, subscriber: Subscriber) -> None:
        self._subscribers[home_id].discard(subscriber)
        if not self._subscribers[home_id]:
            del self._subscribers[home_id]

    async def dispatch(self, event: DeviceEvent) -> None:
        for subscriber in tuple(self._subscribers.get(event.home_id, ())):
            subscriber.offer(event)


@dataclass(frozen=True, slots=True)
class LiveModule:
    hub: LiveHub
    devices: DevicesService
    telemetry: TelemetryQueries
    recheck_every_s: float = 60.0


async def snapshot(
    module: LiveModule, home_id: UUID, scope: frozenset[str] | None
) -> dict[str, Any]:
    devices = []
    for device, live in await module.devices.live(home_id, scope=scope):
        devices.append(
            {
                "id": device.id,
                "kind": device.kind.value,
                "name": device.name,
                "room": device.room,
                "status": device.status.value,
                "online": live["online"],
                "reported": live["reported"] or {},
                "readings": await module.telemetry.latest(device.id),
            }
        )
    return {"type": "snapshot", "devices": devices}


CanWatch = Depends(require_websocket_home_access(Permission.VIEW_HOME))


@router.websocket("/homes/{home_id}/live")
async def live(websocket: WebSocket, home_id: UUID, access: HomeAccess = CanWatch) -> None:
    module: LiveModule = websocket.app.state.live
    scope = access.membership.device_scope
    await websocket.accept()
    subscriber = module.hub.subscribe(home_id, scope=scope)
    sockets_gauge.add(1)
    try:
        await websocket.send_json(await snapshot(module, home_id, scope))
        tasks = {
            asyncio.create_task(_send(websocket, subscriber)),
            asyncio.create_task(_drain(websocket)),
            asyncio.create_task(_recheck(websocket, home_id, module.recheck_every_s)),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(WebSocketDisconnect):
                task.result()
    finally:
        module.hub.unsubscribe(home_id, subscriber)
        sockets_gauge.add(-1)


async def _send(websocket: WebSocket, subscriber: Subscriber) -> None:
    overflow = asyncio.create_task(subscriber.overflowed.wait())
    next_message: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            next_message = asyncio.create_task(subscriber.queue.get())
            done, _ = await asyncio.wait(
                {next_message, overflow}, return_when=asyncio.FIRST_COMPLETED
            )
            if overflow in done:
                next_message.cancel()
                dropped_counter.add(1)
                await websocket.close(CLOSE_TRY_AGAIN_LATER, "too slow; reconnect")
                return
            await websocket.send_json(next_message.result())
    finally:
        overflow.cancel()
        if next_message is not None:
            next_message.cancel()


async def _drain(websocket: WebSocket) -> None:
    """Read (and ignore) client frames, so a disconnect is noticed at once."""
    while True:
        frame = await websocket.receive()
        if frame["type"] == "websocket.disconnect":
            return


async def _recheck(websocket: WebSocket, home_id: UUID, every_s: float) -> None:
    """A socket outlives the request that opened it: close it when the session ends or
    the membership is revoked."""
    while True:
        await asyncio.sleep(every_s)
        refused = await recheck_websocket_access(websocket, home_id, Permission.VIEW_HOME)
        if refused is not None:
            log.info("live_socket_revoked", home_id=str(home_id), reason=refused.reason)
            await websocket.close(refused.code, refused.reason)
            return
