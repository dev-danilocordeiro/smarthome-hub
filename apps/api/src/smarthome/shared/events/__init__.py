"""Device events: what changed on a device, for modules that react to it (ADR 0010).

Producers (devices, telemetry) publish through the `EventPublisher` port after their
change is committed; consumers (automations) read the stream in the worker.
"""

from smarthome.shared.events.model import (
    DeviceEvent,
    DeviceEventKind,
    EventPublisher,
    NullEventPublisher,
)

__all__ = ["DeviceEvent", "DeviceEventKind", "EventPublisher", "NullEventPublisher"]
