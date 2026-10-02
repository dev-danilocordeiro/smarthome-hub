"""Inbound MQTT adapter: presence (incl. Last Will) and reported state → device registry."""

from datetime import datetime
from uuid import UUID

from device_protocol import MessageKind
from smarthome.modules.devices.application.services import DevicesService
from smarthome.modules.devices.domain.errors import UnsupportedState
from smarthome.shared.infrastructure.mqtt_consumer import Handler, Received


def handlers(service: DevicesService) -> dict[MessageKind, Handler]:
    async def presence(message: Received) -> bool:
        # Ordered by the hub's clock, not the device's. The Last Will is composed at
        # connect time and carries no timestamp, so comparing a device-stamped `online`
        # with a hub-stamped `offline` would mix clocks: a device running ahead would
        # make its own Will look stale and stay "online" forever.
        return await service.record_presence(
            home_id=UUID(message.topic.home_id),
            device_id=message.topic.device_id,
            online=message.payload["status"] == "online",
            at=message.received_at,
        )

    async def state(message: Received) -> bool:
        try:
            return await service.record_reported_state(
                home_id=UUID(message.topic.home_id),
                device_id=message.topic.device_id,
                reported=message.payload["reported"],
                at=datetime.fromisoformat(message.payload["ts"]),
            )
        except UnsupportedState:
            return False

    return {MessageKind.PRESENCE: presence, MessageKind.STATE: state}
