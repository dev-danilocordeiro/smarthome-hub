"""The device's side of a command's trace.

The hub sends a W3C `traceparent` as an MQTT 5 user property on every command. A device
continues that trace while it handles the command and sends it back on its acks, so one
trace covers request -> outbox -> broker -> device -> ack -> hub.

Spans are exported only when OTEL_EXPORTER_OTLP_ENDPOINT is set. Without it the context
is still passed along unchanged, so the hub's side of the trace stays connected.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

tracer = trace.get_tracer("smarthome_simulator")


def configure() -> None:
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    # Imported lazily: the SDK and exporter are only needed when exporting.
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": os.environ.get("OTEL_SERVICE_NAME", "smarthome-simulator")}
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(insecure=True)))
    trace.set_tracer_provider(provider)


def context_from(properties: object) -> Context:
    pairs = getattr(properties, "UserProperty", None) or []
    return propagate.extract({str(k): str(v) for k, v in pairs})


def publish_properties() -> Properties:
    """MQTT 5 PUBLISH properties carrying the current trace context."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    properties = Properties(PacketTypes.PUBLISH)  # type: ignore[no-untyped-call]
    if carrier:
        properties.UserProperty = list(carrier.items())
    return properties


@contextmanager
def handling_command(properties: object, *, device_id: str, kind: str) -> Iterator[trace.Span]:
    with tracer.start_as_current_span(
        "device handle command",
        context=context_from(properties),
        kind=trace.SpanKind.CONSUMER,
        attributes={
            "messaging.system": "mqtt",
            "smarthome.device.id": device_id,
            "smarthome.device.kind": kind,
        },
    ) as span:
        yield span


def shutdown() -> None:
    provider = trace.get_tracer_provider()
    shutdown_fn = getattr(provider, "shutdown", None)
    if callable(shutdown_fn):
        shutdown_fn()
