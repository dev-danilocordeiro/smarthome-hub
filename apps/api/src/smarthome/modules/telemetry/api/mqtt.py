"""Inbound MQTT adapter for telemetry (shared subscription across ingestor replicas)."""

from uuid import UUID

from device_protocol import MessageKind
from smarthome.modules.telemetry.application.services import TelemetryIngest
from smarthome.shared.infrastructure.mqtt_consumer import Handler, Received


def handlers(ingest: TelemetryIngest) -> dict[MessageKind, Handler]:
    async def telemetry(message: Received) -> bool:
        return await ingest.accept(
            home_id=UUID(message.topic.home_id),
            device_id=message.topic.device_id,
            payload=message.payload,
            received_at=message.received_at,
        )

    return {MessageKind.TELEMETRY: telemetry}
