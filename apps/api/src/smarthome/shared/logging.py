import logging
import sys
from typing import TYPE_CHECKING

import structlog
from opentelemetry import trace

from smarthome.shared.observability import OtlpLogHandler

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger


def add_trace_context(_: "WrappedLogger", __: str, event_dict: "EventDict") -> "EventDict":
    """Stamp the active span's ids on every record so stdout logs join traces in Grafana."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict


def configure_logging(*, level: str, json: bool, otlp: bool = False) -> None:
    """Route both structlog and stdlib (uvicorn, sqlalchemy) records through one renderer.

    Values are always passed as key/value pairs, never interpolated into the event
    string, so JSON output stays queryable in Loki. With `otlp`, records are also
    exported through the OTel logger provider (see `observability.OtlpLogHandler`).
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        add_trace_context,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    if otlp:
        otlp_handler = OtlpLogHandler()
        # The exporter logs its own failures; exporting those would feed back into itself.
        otlp_handler.addFilter(lambda record: not record.name.startswith("opentelemetry"))
        root.addHandler(otlp_handler)
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # uvicorn's access log interpolates values into the message string. Request logging
    # comes from OpenTelemetry instrumentation instead (phase 2).
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every request URL at INFO. Outbound URLs can carry secrets (a webhook
    # URL with a token in its path, OIDC endpoints with codes in the query): keep only
    # its warnings. Spans record outbound calls without the query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
