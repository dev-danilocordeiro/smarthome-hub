"""Generic consumer of device traffic for the hub (used by the ingestor entrypoint).

Modules register one handler per message kind; this loop owns the connection, MQTT 5
shared subscriptions (so ingestor replicas split the load), schema validation,
per-message spans and metrics, and reconnection.
"""

import asyncio
import contextlib
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiomqtt
import structlog
from aiomqtt import ProtocolVersion
from opentelemetry import metrics, trace

from device_protocol import (
    DeviceTopic,
    InvalidMessage,
    InvalidTopic,
    MessageKind,
    Payload,
    decode,
    hub_subscription,
    parse,
)

log = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

messages_counter = meter.create_counter(
    "smarthome.ingestor.messages",
    unit="{message}",
    description="Device messages consumed, by kind and outcome (accepted, ignored, invalid).",
)
handling_duration = meter.create_histogram(
    "smarthome.ingestor.handling.duration",
    unit="s",
    description="Time to validate and apply one device message.",
)

SHARE_GROUP = "ingestor"
# High-volume kinds are split across ingestor replicas with MQTT 5 shared subscriptions.
# Last-value kinds (presence, state) are NOT shared: the broker never replays retained
# messages to a shared subscription, and replaying them is how a restarted ingestor
# catches up. Every replica applies them; handlers are idempotent and drop stale ones.
SHARED_KINDS = frozenset({MessageKind.TELEMETRY, MessageKind.COMMAND_ACK})


def subscription(kind: MessageKind) -> str:
    return hub_subscription(kind, share_group=SHARE_GROUP if kind in SHARED_KINDS else None)


MAX_BACKOFF_S = 30.0


class SubscriptionRefused(RuntimeError):
    """Fatal: the hub account lacks an ACL. Retrying cannot fix configuration."""


@dataclass(frozen=True, slots=True)
class Received:
    topic: DeviceTopic
    payload: Payload
    received_at: datetime


# Returns True if the message changed something, False if it was ignored
# (unknown device, stale, inactive...).
Handler = Callable[[Received], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class ConsumerConfig:
    host: str
    port: int
    ca_file: str
    username: str
    password: str
    heartbeat_file: Path | None = None


def subscription_acls() -> list[dict[str, object]]:
    """What the hub account needs. Mosquitto checks shared subscriptions against the full
    `$share/<group>/...` filter, so each kind is granted exactly as it is subscribed."""
    consumed = [k for k in MessageKind if k is not MessageKind.COMMAND]
    subscribe = [
        {"acltype": "subscribePattern", "topic": subscription(k), "allow": True} for k in consumed
    ]
    publish = {
        "acltype": "publishClientSend",
        "topic": hub_subscription(MessageKind.COMMAND),
        "allow": True,
    }
    return [*subscribe, publish]


class DeviceTrafficConsumer:
    def __init__(self, config: ConsumerConfig, handlers: Mapping[MessageKind, Handler]) -> None:
        self._config = config
        self._handlers = dict(handlers)
        self._client_id = f"ingestor-{socket.gethostname()}"[:64]

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                await self._session(stop)
                return
            except (aiomqtt.MqttError, OSError) as exc:
                log.warning("ingestor_connection_lost", error=str(exc), retry_in_s=backoff)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _session(self, stop: asyncio.Event) -> None:
        context = ssl.create_default_context(cafile=self._config.ca_file)
        async with aiomqtt.Client(
            self._config.host,
            self._config.port,
            username=self._config.username,
            password=self._config.password,
            identifier=self._client_id,
            protocol=ProtocolVersion.V5,
            tls_context=context,
            # Clean session on purpose. With shared subscriptions, a persistent session of a
            # replica that died keeps receiving its share of messages until it expires, so
            # they would be stranded. Starting clean loses nothing that matters: presence
            # and state are retained and replayed on (non-shared) subscribe, telemetry is
            # QoS 0 (never queued), and every handler is idempotent.
            clean_start=True,
            timeout=10,
        ) as client:
            for kind in self._handlers:
                granted = await client.subscribe(subscription(kind), qos=1)
                if any(getattr(code, "is_failure", False) for code in granted):
                    # Without this check an ACL mistake looks like a quiet broker.
                    raise SubscriptionRefused(f"broker refused {subscription(kind)}: {granted}")
            log.info("ingestor_subscribed", kinds=[k.value for k in self._handlers])
            consume = asyncio.create_task(self._consume(client))
            beat = asyncio.create_task(self._heartbeat())
            waiter = asyncio.create_task(stop.wait())
            done, pending = await asyncio.wait(
                {consume, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in (*pending, beat):
                task.cancel()
            if consume in done:
                consume.result()  # surface the connection error to trigger a reconnect

    async def _consume(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            await self.handle(str(message.topic), message.payload)

    async def handle(self, topic_name: str, raw: object) -> str:
        """Validate and dispatch one message; returns the outcome (also used by tests)."""
        started = time.perf_counter()
        kind = "unknown"
        outcome = "invalid"
        with tracer.start_as_current_span("ingest device message") as span:
            try:
                device_topic = parse(topic_name)
                kind = device_topic.kind.value
                span.set_attributes(
                    {
                        "messaging.system": "mqtt",
                        "messaging.destination.name": topic_name,
                        "smarthome.device.id": device_topic.device_id,
                        "smarthome.home.id": device_topic.home_id,
                    }
                )
                handler = self._handlers.get(device_topic.kind)
                if handler is None:
                    outcome = "ignored"
                else:
                    payload = decode(device_topic.kind, raw if isinstance(raw, bytes) else b"")
                    received = Received(device_topic, payload, datetime.now(UTC))
                    outcome = "accepted" if await handler(received) else "ignored"
            except (InvalidTopic, InvalidMessage, ValueError) as exc:
                log.info("device_message_rejected", topic=topic_name, reason=str(exc))
                span.set_status(trace.StatusCode.ERROR, "invalid device message")
            span.set_attribute("smarthome.ingest.outcome", outcome)
        attributes = {"kind": kind, "outcome": outcome}
        messages_counter.add(1, attributes)
        handling_duration.record(time.perf_counter() - started, attributes)
        return outcome

    async def _heartbeat(self) -> None:
        """Touch a file the container healthcheck looks at: alive *and* consuming."""
        if self._config.heartbeat_file is None:
            return
        while True:
            self._config.heartbeat_file.touch()
            await asyncio.sleep(5)
