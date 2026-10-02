"""The hub's outbound MQTT connection (used by the worker's outbox relay).

One long-lived MQTT 5 connection, opened on first use and reopened after a failure. The
trace context goes out as MQTT 5 user properties, so a device can continue the trace.
"""

import asyncio
import contextlib
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass

import aiomqtt
import structlog
from aiomqtt import ProtocolVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

log = structlog.get_logger(__name__)

PUBLISH_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class PublisherConfig:
    host: str
    port: int
    ca_file: str
    username: str
    password: str
    client_id_prefix: str = "worker"


class MqttPublisher:
    def __init__(self, config: PublisherConfig) -> None:
        self._config = config
        self._client_id = f"{config.client_id_prefix}-{socket.gethostname()}"[:64]
        self._stack: contextlib.AsyncExitStack | None = None
        self._client: aiomqtt.Client | None = None
        self._lock = asyncio.Lock()

    async def publish(
        self, topic: str, payload: bytes, *, qos: int, headers: Mapping[str, str]
    ) -> None:
        client = await self._connected()
        properties = Properties(PacketTypes.PUBLISH)  # type: ignore[no-untyped-call]
        if headers:
            properties.UserProperty = list(headers.items())
        try:
            await client.publish(
                topic, payload, qos=qos, properties=properties, timeout=PUBLISH_TIMEOUT_S
            )
        except (aiomqtt.MqttError, OSError):
            await self.close()  # reconnect on the next attempt
            raise

    async def _connected(self) -> aiomqtt.Client:
        async with self._lock:
            if self._client is not None:
                return self._client
            stack = contextlib.AsyncExitStack()
            config = self._config
            context = ssl.create_default_context(cafile=config.ca_file)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            client = await stack.enter_async_context(
                aiomqtt.Client(
                    config.host,
                    config.port,
                    username=config.username,
                    password=config.password,
                    identifier=self._client_id,
                    protocol=ProtocolVersion.V5,
                    tls_context=context,
                    timeout=PUBLISH_TIMEOUT_S,
                )
            )
            self._stack, self._client = stack, client
            log.info("mqtt_publisher_connected", client_id=self._client_id)
            return client

    async def close(self) -> None:
        async with self._lock:
            stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            with contextlib.suppress(aiomqtt.MqttError, OSError):
                await stack.aclose()
