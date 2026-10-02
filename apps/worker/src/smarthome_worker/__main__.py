"""Composition root for the `worker` process (ADR 0001).

Background work over the same modules as the API:
- the outbox relay: publishes committed outbox messages (device commands) to the broker;
- the command sweeper: settles commands no device answered in time;
- outbox housekeeping: deletes processed messages after the retention window.

Plain asyncio loops, no job queue (ADR 0009). Several replicas can run side by side.
"""

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import structlog

from smarthome.modules.commands import wiring as commands
from smarthome.modules.devices import wiring as devices
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings, get_settings
from smarthome.shared.infrastructure.db import create_engine
from smarthome.shared.infrastructure.mqtt_publisher import MqttPublisher, PublisherConfig
from smarthome.shared.infrastructure.outbox import OutboxRelay
from smarthome.shared.infrastructure.redis import create_redis
from smarthome.shared.logging import configure_logging
from smarthome.shared.observability import configure_providers

log = structlog.get_logger(__name__)

HEARTBEAT_FILE = Path("/tmp/worker-heartbeat")  # noqa: S108 - tmpfs in the container
PURGE_EVERY_S = 3600.0


async def every(
    seconds: float, job: Callable[[], Awaitable[object]], stop: asyncio.Event, *, name: str
) -> None:
    """Run `job` now and then every `seconds` until `stop`; a failing run is logged and
    the schedule goes on."""
    while not stop.is_set():
        try:
            await job()
        except Exception:
            log.exception("periodic_job_failed", job=name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)


async def heartbeat() -> None:
    await asyncio.to_thread(HEARTBEAT_FILE.touch)


async def run(settings: Settings) -> None:
    engine = create_engine(settings)
    redis = create_redis(settings)
    clock = SystemClock()
    publisher = MqttPublisher(
        PublisherConfig(
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            ca_file=settings.mqtt_ca_file,
            username=settings.mqtt_hub_user,
            password=settings.mqtt_hub_password.get_secret_value(),
        )
    )
    try:
        relay = OutboxRelay(
            engine,
            publisher,
            clock,
            batch_size=settings.outbox_batch_size,
            poll_interval_s=settings.outbox_poll_interval_s,
        )
        device_service = devices.build_service(settings, engine=engine, redis=redis, clock=clock)
        command_service = commands.build_service(
            settings, engine=engine, devices=device_service, clock=clock
        )
        retention = timedelta(hours=settings.outbox_retention_hours)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        log.info("worker_started", broker=f"{settings.mqtt_host}:{settings.mqtt_port}")
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(relay.run(stop))
            tasks.create_task(
                every(
                    settings.command_sweep_interval_s,
                    command_service.expire_overdue,
                    stop,
                    name="expire_commands",
                )
            )
            tasks.create_task(
                every(
                    PURGE_EVERY_S,
                    lambda: relay.purge_processed(older_than=retention),
                    stop,
                    name="purge_outbox",
                )
            )
            tasks.create_task(every(5, heartbeat, stop, name="heartbeat"))
    finally:
        await publisher.close()
        await redis.aclose()
        await engine.dispose()
        log.info("worker_stopped")


def main() -> None:
    settings = get_settings()
    providers = (
        configure_providers(settings, service_name="smarthome-worker")
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
