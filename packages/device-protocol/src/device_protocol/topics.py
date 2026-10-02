"""MQTT topic layout, version 1.

    v1/homes/{home_id}/devices/{device_id}/{telemetry|state|commands|commands/ack|presence}

The version is the first segment so a v2 layout can run side by side during a
migration, with its own ACLs.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

TOPIC_VERSION = "v1"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# v1 / homes / {home} / devices / {device} / kind (kind itself may contain a slash)
_MIN_SEGMENTS = 6


class InvalidTopic(ValueError):
    pass


class MessageKind(StrEnum):
    TELEMETRY = "telemetry"
    STATE = "state"
    COMMAND = "commands"
    COMMAND_ACK = "commands/ack"
    PRESENCE = "presence"


@dataclass(frozen=True, slots=True)
class Delivery:
    """How a message kind travels. Justified in docs/adr/0005-mqtt-broker-and-qos.md."""

    qos: int
    retain: bool
    sender: str  # "device" or "hub"


DELIVERY: dict[MessageKind, Delivery] = {
    # High volume and superseded by the next sample: losing one is cheaper than acking all.
    MessageKind.TELEMETRY: Delivery(qos=0, retain=False, sender="device"),
    # Last known state must survive reconnects: new subscribers get it immediately.
    MessageKind.STATE: Delivery(qos=1, retain=True, sender="device"),
    # Must arrive; duplicates are handled by command_id idempotency on the device.
    MessageKind.COMMAND: Delivery(qos=1, retain=False, sender="hub"),
    MessageKind.COMMAND_ACK: Delivery(qos=1, retain=False, sender="device"),
    # Retained so "is it online?" has an answer without waiting for the next heartbeat.
    MessageKind.PRESENCE: Delivery(qos=1, retain=True, sender="device"),
}


def _check_id(value: str, what: str) -> str:
    if not _ID.match(value):
        raise InvalidTopic(f"{what} must match {_ID.pattern}, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class DeviceTopic:
    home_id: str
    device_id: str
    kind: MessageKind

    def __post_init__(self) -> None:
        _check_id(self.home_id, "home_id")
        _check_id(self.device_id, "device_id")

    def __str__(self) -> str:
        return f"{TOPIC_VERSION}/homes/{self.home_id}/devices/{self.device_id}/{self.kind.value}"

    @property
    def delivery(self) -> Delivery:
        return DELIVERY[self.kind]


def topic(home_id: str, device_id: str, kind: MessageKind) -> str:
    return str(DeviceTopic(home_id, device_id, kind))


def parse(value: str) -> DeviceTopic:
    parts = value.split("/")
    if (
        len(parts) < _MIN_SEGMENTS
        or parts[0] != TOPIC_VERSION
        or parts[1] != "homes"
        or parts[3] != "devices"
    ):
        raise InvalidTopic(f"not a {TOPIC_VERSION} device topic: {value!r}")
    try:
        kind = MessageKind("/".join(parts[5:]))
    except ValueError as exc:
        raise InvalidTopic(f"unknown message kind in {value!r}") from exc
    return DeviceTopic(home_id=parts[2], device_id=parts[4], kind=kind)


def device_publishes(home_id: str, device_id: str) -> list[str]:
    """Topics a device may publish to (its broker ACL)."""
    return [topic(home_id, device_id, k) for k, d in DELIVERY.items() if d.sender == "device"]


def device_subscribes(home_id: str, device_id: str) -> list[str]:
    """Topics a device may subscribe to (its broker ACL)."""
    return [topic(home_id, device_id, MessageKind.COMMAND)]


def hub_subscription(kind: MessageKind, *, share_group: str | None = None) -> str:
    """Wildcard subscription for the hub. With `share_group`, MQTT 5 shared subscriptions
    spread messages across several ingestor replicas instead of copying them to each."""
    pattern = f"{TOPIC_VERSION}/homes/+/devices/+/{kind.value}"
    return f"$share/{share_group}/{pattern}" if share_group else pattern
