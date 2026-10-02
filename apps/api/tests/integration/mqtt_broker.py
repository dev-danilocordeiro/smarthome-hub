"""Mosquitto from the project's own config and entrypoint, with freshly generated certs."""

import asyncio
import os
import ssl
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiomqtt
import pytest
from aiomqtt import ProtocolVersion, Will
from testcontainers.core.container import DockerContainer

from smarthome.modules.devices.infrastructure.broker_admin import BrokerAdmin, BrokerConnection

# Keep in sync with infra/docker-compose.yml.
MOSQUITTO_IMAGE = "eclipse-mosquitto:2.1.2-alpine"
ADMIN_USER = "hub-admin"
ADMIN_PASSWORD = "integration-admin"
STARTUP_TIMEOUT = 60


@dataclass(frozen=True)
class Broker:
    host: str
    port: int
    ca_file: str

    def tls(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=self.ca_file)

    def admin(self) -> BrokerAdmin:
        return BrokerAdmin(
            BrokerConnection(self.host, self.port, self.ca_file, ADMIN_USER, ADMIN_PASSWORD)
        )

    @asynccontextmanager
    async def connect(
        self,
        username: str,
        password: str,
        *,
        client_id: str | None = None,
        will: Will | None = None,
    ) -> AsyncIterator[aiomqtt.Client]:
        async with aiomqtt.Client(
            self.host,
            self.port,
            username=username,
            password=password,
            identifier=client_id or username,
            protocol=ProtocolVersion.V5,
            tls_context=self.tls(),
            will=will,
            timeout=5,
        ) as client:
            yield client


@pytest.fixture(scope="session")
def broker(tmp_path_factory: pytest.TempPathFactory, repo_root: Path) -> Iterator[Broker]:
    certs = tmp_path_factory.mktemp("mqtt-certs")
    subprocess.run(
        [str(repo_root / "scripts" / "gen-dev-certs.sh")],
        env={**os.environ, "CERT_DIR": str(certs)},
        check=True,
        capture_output=True,
    )
    certs.chmod(0o755)  # the broker runs as uid 1883 inside the container

    container = (
        DockerContainer(MOSQUITTO_IMAGE)
        .with_env("MQTT_ADMIN_USER", ADMIN_USER)
        .with_env("MQTT_ADMIN_PASSWORD", ADMIN_PASSWORD)
        .with_exposed_ports(8883)
        .with_volume_mapping(
            str(repo_root / "infra/mqtt/mosquitto.conf"), "/mosquitto/config/mosquitto.conf", "ro"
        )
        .with_volume_mapping(
            str(repo_root / "infra/mqtt/docker-entrypoint.sh"), "/entrypoint.sh", "ro"
        )
        .with_volume_mapping(str(certs), "/mosquitto/certs", "ro")
        .with_kwargs(entrypoint=["/entrypoint.sh"])
    )
    with container:
        broker = Broker(
            host="localhost",
            port=int(container.get_exposed_port(8883)),
            ca_file=str(certs / "ca.crt"),
        )
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            try:
                asyncio.run(_ping(broker))
                break
            except (aiomqtt.MqttError, OSError):
                if time.monotonic() > deadline:
                    raise TimeoutError(container.get_logs()) from None
                time.sleep(0.5)
        yield broker


async def _ping(broker: Broker) -> None:
    async with broker.connect(ADMIN_USER, ADMIN_PASSWORD):
        pass
