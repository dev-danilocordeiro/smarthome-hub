"""Device protocol v1: MQTT topics, delivery guarantees and message schemas.

Shared by the hub, the simulator and real firmware. See docs/device-protocol.md.
"""

from device_protocol.messages import (
    MAX_PAYLOAD_BYTES,
    SCHEMA_VERSION,
    InvalidMessage,
    Payload,
    decode,
    encode,
    schema,
    validate,
)
from device_protocol.topics import (
    DELIVERY,
    TOPIC_VERSION,
    Delivery,
    DeviceTopic,
    InvalidTopic,
    MessageKind,
    device_publishes,
    device_subscribes,
    hub_subscription,
    parse,
    topic,
)

PROTOCOL_VERSION = "1"

__all__ = [
    "DELIVERY",
    "MAX_PAYLOAD_BYTES",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "TOPIC_VERSION",
    "Delivery",
    "DeviceTopic",
    "InvalidMessage",
    "InvalidTopic",
    "MessageKind",
    "Payload",
    "decode",
    "device_publishes",
    "device_subscribes",
    "encode",
    "hub_subscription",
    "parse",
    "schema",
    "topic",
    "validate",
]
