"""Inbound MQTT adapter: command acks (shared subscription across ingestor replicas)."""

from uuid import UUID

from device_protocol import MessageKind
from smarthome.modules.commands.application.services import CommandsService
from smarthome.modules.commands.domain.model import AckStatus
from smarthome.shared.infrastructure.mqtt_consumer import Handler, Received


def handlers(service: CommandsService) -> dict[MessageKind, Handler]:
    async def command_ack(message: Received) -> bool:
        return await service.record_ack(
            home_id=UUID(message.topic.home_id),
            device_id=message.topic.device_id,
            command_id=UUID(message.payload["command_id"]),
            ack=AckStatus(message.payload["status"]),
            reason=message.payload.get("reason") or None,
        )

    return {MessageKind.COMMAND_ACK: command_ack}
