"""OpenTelemetry wiring shared by every entrypoint (api, ingestor, worker).

Two separate steps:

* `configure_providers` installs the process-wide tracer, meter and logger providers
  with OTLP exporters. Each entrypoint calls it once at startup when OTel is enabled.
  Tests install in-memory providers instead.
* `instrument_app` / `instrument_engine` / `instrument_redis` attach instrumentation.
  It always runs: without real providers the OTel API is a no-op, so the code paths
  stay the same whether telemetry is exported or not.
"""

import logging
import os
import socket
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opentelemetry import _logs as otel_logs
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

    from smarthome.shared.config import Settings

# Probes and scrapes would otherwise dominate traces and RED metrics.
EXCLUDED_URLS = "/health/live,/health/ready"

# Only process-level metrics; host metrics belong to the infrastructure, not the service.
SYSTEM_METRICS_CONFIG: dict[str, list[str] | None] = {
    "process.cpu.utilization": None,
    "process.memory.usage": None,
    "process.open_file_descriptor.count": None,
    "process.thread.count": None,
}


@dataclass(frozen=True, slots=True)
class Providers:
    tracer: TracerProvider
    meter: MeterProvider
    logger: LoggerProvider

    def shutdown(self) -> None:
        # Flush order matters: logs and spans reference the trace context, metrics do not.
        self.logger.shutdown()
        self.tracer.shutdown()
        self.meter.shutdown()


# The SDK's default histogram boundaries (0, 5, 10, 25 ... 10000) are meant for
# milliseconds. Every duration here is in seconds, which would put nearly every sample in
# the first bucket and make every quantile read "about 5 s". One view for all of them,
# from 5 ms (an MQTT handler) to an hour (the energy rollup's lag).
SECONDS_BOUNDARIES = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75,
    1.0, 2.5, 5.0, 7.5, 10.0, 30.0, 60.0, 300.0, 900.0, 1800.0, 3600.0,
)  # fmt: skip
SECONDS_HISTOGRAMS = View(
    instrument_type=Histogram,
    instrument_unit="s",
    aggregation=ExplicitBucketHistogramAggregation(SECONDS_BOUNDARIES),
)


def build_resource(settings: "Settings", *, service_name: str | None = None) -> Resource:
    return Resource.create(
        {
            "service.name": service_name or settings.service_name,
            "service.namespace": "smarthome",
            "service.version": settings.service_version,
            "service.instance.id": f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}",
            "deployment.environment.name": settings.environment.value,
        }
    )


def configure_providers(settings: "Settings", *, service_name: str | None = None) -> Providers:
    """Install global OTLP-exporting providers. Call once per process."""
    resource = build_resource(settings, service_name=service_name)
    endpoint = settings.otel_exporter_otlp_endpoint
    insecure = settings.otel_exporter_otlp_insecure

    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sample_ratio)),
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
    )

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=insecure),
                export_interval_millis=settings.otel_metric_export_interval_ms,
            )
        ],
        views=[SECONDS_HISTOGRAMS],
    )

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=insecure))
    )

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    otel_logs.set_logger_provider(logger_provider)
    return Providers(tracer=tracer_provider, meter=meter_provider, logger=logger_provider)


def use_stable_semantic_conventions() -> None:
    """Opt in to the stable HTTP and DB conventions (`http.server.request.duration` in
    seconds, `http.route`, `db.system.name`, ...). The instrumentations read this once,
    when they are first applied, so it has to be set before any of them runs."""
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database")


def instrument_app(app: "FastAPI") -> None:
    use_stable_semantic_conventions()
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)


def instrument_engine(engine: "AsyncEngine") -> None:
    use_stable_semantic_conventions()
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, enable_commenter=False)


def instrument_redis() -> None:
    """Redis instrumentation patches the client class, so it is process-wide and idempotent."""
    instrumentor = RedisInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument()


def instrument_process_metrics() -> None:
    instrumentor = SystemMetricsInstrumentor(config=SYSTEM_METRICS_CONFIG)
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument()


@contextmanager
def untraced() -> Iterator[None]:
    """Run a block without creating spans (e.g. readiness probes that hit the database)."""
    with suppress_instrumentation():
        yield


# --- Logs -----------------------------------------------------------------------

_SEVERITY = {
    logging.DEBUG: otel_logs.SeverityNumber.DEBUG,
    logging.INFO: otel_logs.SeverityNumber.INFO,
    logging.WARNING: otel_logs.SeverityNumber.WARN,
    logging.ERROR: otel_logs.SeverityNumber.ERROR,
    logging.CRITICAL: otel_logs.SeverityNumber.FATAL,
}
_SEVERITY_TEXT = {logging.WARNING: "WARN", logging.CRITICAL: "FATAL"}

# Keys structlog adds that OTLP already carries natively (timestamp, level, trace context).
_REDUNDANT_KEYS = frozenset({"event", "timestamp", "level", "trace_id", "span_id", "logger"})

AttributeValue = str | bool | int | float


class OtlpLogHandler(logging.Handler):
    """Bridge stdlib/structlog records to the OTel Logs API.

    The structlog `event` becomes the log body and every other key becomes an attribute,
    so Loki can filter on them as structured metadata. Records emitted inside a span
    carry its trace context automatically, which is what links logs to traces.
    """

    def __init__(
        self, level: int = logging.NOTSET, logger_provider: otel_logs.LoggerProvider | None = None
    ) -> None:
        super().__init__(level)
        self._logger_provider = logger_provider

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event, attributes = _split_event(record)
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                attributes["exception.type"] = type(exc).__name__
                attributes["exception.message"] = str(exc)
            otel_logs.get_logger(record.name, logger_provider=self._logger_provider).emit(
                timestamp=int(record.created * 1e9),
                observed_timestamp=time.time_ns(),
                severity_number=_SEVERITY.get(record.levelno, otel_logs.SeverityNumber.INFO),
                severity_text=_SEVERITY_TEXT.get(record.levelno, record.levelname),
                body=event,
                attributes=attributes,
            )
        except Exception:  # noqa: BLE001 - logging must never take the process down
            self.handleError(record)


def _split_event(record: logging.LogRecord) -> tuple[str, dict[str, AttributeValue]]:
    attributes: dict[str, AttributeValue] = {"logger.name": record.name}
    if isinstance(record.msg, Mapping):
        # structlog's ProcessorFormatter.wrap_for_formatter puts the event dict in `msg`.
        event_dict: Mapping[str, Any] = record.msg
        event = str(event_dict.get("event", ""))
        for key, value in event_dict.items():
            if key in _REDUNDANT_KEYS or key.startswith("_"):
                continue
            attributes[key] = _to_attribute(value)
        return event, attributes
    return record.getMessage(), attributes


def _to_attribute(value: object) -> AttributeValue:
    if isinstance(value, str | bool | int | float):
        return value
    return str(value)
