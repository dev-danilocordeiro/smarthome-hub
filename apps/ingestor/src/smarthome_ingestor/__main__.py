"""Composition root for the `ingestor` process (ADR 0001).

Consumes device traffic from the broker and hands each message kind to the module that
owns it. Same modules as the API, different entrypoint. Telemetry (phase 6) plugs in here.
"""

import asyncio
import signal
from pathlib import Path

import structlog

from smarthome.modules.devices import wiring as devices
from smarthome.modules.devices.api.mqtt import handlers as device_handlers
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings, get_settings
from smarthome.shared.infrastructure.db import create_engine
from smarthome.shared.infrastructure.mqtt_consumer import (
    ConsumerConfig,
    DeviceTrafficConsumer,
    subscription_acls,
)
from smarthome.shared.infrastructure.redis import create_redis
from smarthome.shared.logging import configure_logging
from smarthome.shared.observability import configure_providers, instrument_redis

log = structlog.get_logger(__name__)

HEARTBEAT_FILE = Path("/tmp/ingestor-heartbeat")  # noqa: S108 - tmpfs in the container


async def ensure_hub_account(settings: Settings) -> None:
    """Create or reconcile the hub's MQTT identity (ACLs and password) on every start."""
    await devices.broker_admin(settings).ensure_service_account(
        username=settings.mqtt_hub_user,
        password=settings.mqtt_hub_password.get_secret_value(),
        acls=subscription_acls(),
    )
    log.info("hub_mqtt_account_ready", username=settings.mqtt_hub_user)


async def run(settings: Settings) -> None:
    engine = create_engine(settings)
    redis = create_redis(settings)
    instrument_redis()
    try:
        await ensure_hub_account(settings)
        service = devices.build_service(settings, engine=engine, redis=redis, clock=SystemClock())
        consumer = DeviceTrafficConsumer(
            ConsumerConfig(
                host=settings.mqtt_host,
                port=settings.mqtt_port,
                ca_file=settings.mqtt_ca_file,
                username=settings.mqtt_hub_user,
                password=settings.mqtt_hub_password.get_secret_value(),
                heartbeat_file=HEARTBEAT_FILE,
            ),
            handlers=device_handlers(service),
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        log.info("ingestor_started", broker=f"{settings.mqtt_host}:{settings.mqtt_port}")
        await consumer.run(stop)
    finally:
        await redis.aclose()
        await engine.dispose()
        log.info("ingestor_stopped")


def main() -> None:
    settings = get_settings()
    providers = (
        configure_providers(settings, service_name="smarthome-ingestor")
        if settings.otel_enabled
        else None
    )
    configure_logging(level=settings.log_level, json=settings.log_json, otlp=providers is not None)
    try:
        asyncio.run(run(settings))
    finally:
        if providers is not None:
            providers.shutdown()


if __name__ == "__main__":
    main()
