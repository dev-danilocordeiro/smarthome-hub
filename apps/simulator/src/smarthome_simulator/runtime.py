"""Runs simulated devices against the broker, one MQTT 5 connection per device.

Each device connects with its own credentials over TLS, registers a Last Will (retained
`offline` presence), announces itself online, publishes telemetry on its own cadence and
answers commands. Faults (abrupt connection loss) exercise the hub's offline detection.
"""

import asyncio
import contextlib
import json
import logging
import random
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime

import aiomqtt
from aiomqtt import ProtocolVersion, Will
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from device_protocol import DELIVERY, MessageKind, encode, topic
from smarthome_simulator import tracing
from smarthome_simulator.devices import SimulatedDevice

log = logging.getLogger("smarthome_simulator")

# Keep the MQTT session (and queued QoS 1 commands) for an hour while offline.
SESSION_EXPIRY_S = 3600
KEEPALIVE_S = 30
MAX_BACKOFF_S = 60.0
DISCONNECT_WITH_WILL = ReasonCode(PacketTypes.DISCONNECT, identifier=0x04)


class ConnectionDropped(Exception):
    """Injected fault: the device vanished without a DISCONNECT packet."""


class Stopping(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BrokerEndpoint:
    host: str
    port: int
    ca_file: str

    def tls_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=self.ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context


def now() -> datetime:
    return datetime.now(UTC)


def _event(event: str, **fields: object) -> None:
    log.info(json.dumps({"event": event, **fields}, default=str))


class DeviceRunner:
    def __init__(
        self,
        device: SimulatedDevice,
        password: str,
        broker: BrokerEndpoint,
        *,
        rate: float = 1.0,
    ) -> None:
        self.device = device
        # Telemetry messages per nominal interval (load tests); simulated time is `speed`.
        self._rate = rate
        self._password = password
        self._broker = broker
        self._rng = random.Random(device.device_id)  # noqa: S311 - simulation
        # Set when a fault closed the socket on purpose: whatever error aiomqtt raises on
        # the way out (it still tries to send DISCONNECT) is part of the fault, not a failure.
        self._dropped = False

    def _topic(self, kind: MessageKind) -> str:
        return topic(self.device.home_id, self.device.device_id, kind)

    async def _publish(
        self,
        client: aiomqtt.Client,
        kind: MessageKind,
        payload: dict[str, object],
        properties: Properties | None = None,
    ) -> None:
        delivery = DELIVERY[kind]
        await client.publish(
            self._topic(kind),
            encode(kind, payload),
            qos=delivery.qos,
            retain=delivery.retain,
            properties=properties,
        )

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        reason = "boot"
        while not stop.is_set():
            try:
                await self._session(stop, reason)
                return
            except* (ConnectionDropped, aiomqtt.MqttError, OSError) as group:
                if self._dropped:
                    self._dropped = False
                    _event("fault_connection_dropped", device_id=self.device.device_id)
                    await self._sleep(stop, self._rng.uniform(5, 30))
                else:
                    _event(
                        "connection_failed",
                        device_id=self.device.device_id,
                        error=str(group.exceptions[0]),
                        retry_in_s=backoff,
                    )
                    await self._sleep(stop, backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
            reason = "reconnect"

    async def _session(self, stop: asyncio.Event, reason: str) -> None:
        device = self.device
        will = Will(
            topic=self._topic(MessageKind.PRESENCE),
            payload=encode(MessageKind.PRESENCE, device.presence("offline", "connection_lost")),
            qos=1,
            retain=True,
        )
        properties = Properties(PacketTypes.CONNECT)  # type: ignore[no-untyped-call]
        properties.SessionExpiryInterval = SESSION_EXPIRY_S
        async with aiomqtt.Client(
            self._broker.host,
            self._broker.port,
            username=device.device_id,
            password=self._password,
            identifier=device.device_id,
            protocol=ProtocolVersion.V5,
            tls_context=self._broker.tls_context(),
            will=will,
            keepalive=KEEPALIVE_S,
            clean_start=False,
            properties=properties,
            timeout=5,
        ) as client:
            await client.subscribe(self._topic(MessageKind.COMMAND), qos=1)
            await self._publish(
                client, MessageKind.PRESENCE, device.presence("online", reason, now())
            )
            if state := device.state_message(now()):
                await self._publish(client, MessageKind.STATE, state)
            _event(
                "device_online", device_id=device.device_id, kind=device.kind.value, reason=reason
            )
            try:
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self._telemetry(client))
                    tasks.create_task(self._commands(client))
                    tasks.create_task(self._faults(client))
                    tasks.create_task(self._watch(stop))
            except* Stopping:
                await self._publish(
                    client, MessageKind.PRESENCE, device.presence("offline", "shutdown", now())
                )
                _event("device_offline", device_id=device.device_id, reason="shutdown")

    async def _telemetry(self, client: aiomqtt.Client) -> None:
        interval = self.device.spec.telemetry_every_s / self._rate
        # Spread devices so a fleet does not publish in lockstep.
        await asyncio.sleep(self._rng.uniform(0, interval))
        last = now()
        while True:
            current = now()
            dt = (current - last).total_seconds()
            last = current
            if self.device.tick(current) and (state := self.device.state_message(current)):
                await self._publish(client, MessageKind.STATE, state)
            payload = self.device.telemetry_message(current, dt)
            delivery = DELIVERY[MessageKind.TELEMETRY]
            # Not `encode`: a malformed-payload fault must reach the hub unvalidated.
            await client.publish(
                self._topic(MessageKind.TELEMETRY),
                json.dumps(payload, separators=(",", ":")),
                qos=delivery.qos,
            )
            await asyncio.sleep(interval * self._rng.uniform(0.9, 1.1))

    async def _commands(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            raw = message.payload if isinstance(message.payload, bytes) else b""
            with tracing.handling_command(
                message.properties,
                device_id=self.device.device_id,
                kind=self.device.kind.value,
            ) as span:
                outcome = self.device.handle_command(raw, now())
                if outcome.acks:
                    span.set_attribute("smarthome.command.status", outcome.acks[-1]["status"])
                for ack in outcome.acks:
                    await self._publish(
                        client, MessageKind.COMMAND_ACK, ack, tracing.publish_properties()
                    )
            if outcome.state_changed and (state := self.device.state_message(now())):
                await self._publish(client, MessageKind.STATE, state)
            if outcome.acks:
                _event(
                    "command_handled",
                    device_id=self.device.device_id,
                    command_id=outcome.acks[-1]["command_id"],
                    status=outcome.acks[-1]["status"],
                )

    async def _faults(self, client: aiomqtt.Client) -> None:
        rate = self.device.faults.drops_per_hour
        if rate <= 0:
            await asyncio.Event().wait()  # never
        await asyncio.sleep(self._rng.expovariate(rate / 3600))
        # Emulate power loss: MQTT 5 reason code 0x04 ("disconnect with Will message")
        # makes the broker publish the Last Will, exactly as if the device had vanished.
        # aiomqtt does not expose reason codes on disconnect, hence the paho client.
        self._dropped = True
        client._client.disconnect(reasoncode=DISCONNECT_WITH_WILL)
        raise ConnectionDropped

    @staticmethod
    async def _watch(stop: asyncio.Event) -> None:
        await stop.wait()
        raise Stopping

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
