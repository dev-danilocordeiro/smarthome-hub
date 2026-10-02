import logging
from collections.abc import Iterator

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider

from smarthome.shared.logging import add_trace_context
from smarthome.shared.observability import OtlpLogHandler

tracer = TracerProvider().get_tracer(__name__)


def test_records_logged_inside_a_span_carry_its_trace_and_span_ids() -> None:
    with tracer.start_as_current_span("work") as span:
        event = add_trace_context(None, "info", {"event": "something_happened"})
        context = span.get_span_context()

    assert event["trace_id"] == format(context.trace_id, "032x")
    assert event["span_id"] == format(context.span_id, "016x")


def test_records_logged_outside_a_span_have_no_trace_ids() -> None:
    event = add_trace_context(None, "info", {"event": "something_happened"})

    assert "trace_id" not in event
    assert "span_id" not in event


@pytest.fixture
def exported() -> Iterator[tuple[logging.Logger, InMemoryLogRecordExporter]]:
    exporter = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]  # untyped upstream
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    logger = logging.getLogger("tests.otlp")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = OtlpLogHandler(logger_provider=provider)
    logger.addHandler(handler)
    yield logger, exporter
    logger.removeHandler(handler)


def test_a_structlog_event_becomes_the_otlp_body_and_its_keys_become_attributes(
    exported: tuple[logging.Logger, InMemoryLogRecordExporter],
) -> None:
    logger, exporter = exported
    event_dict = {
        "event": "device_registered",
        "device_id": "d-42",
        "battery": 87,
        "tags": ["a", "b"],
        "timestamp": "ignored",
        "level": "info",
    }
    # This is the shape structlog.stdlib.ProcessorFormatter.wrap_for_formatter produces.
    logger.info(event_dict)

    (record,) = [r.log_record for r in exporter.get_finished_logs()]
    assert record.body == "device_registered"
    assert record.attributes is not None
    assert record.attributes["device_id"] == "d-42"
    assert record.attributes["battery"] == 87
    assert record.attributes["tags"] == "['a', 'b']"
    assert "timestamp" not in record.attributes
    assert "level" not in record.attributes
    assert record.severity_text == "INFO"


def test_a_plain_stdlib_record_is_exported_with_its_formatted_message(
    exported: tuple[logging.Logger, InMemoryLogRecordExporter],
) -> None:
    logger, exporter = exported

    logger.warning("connection to %s lost", "broker")

    (record,) = [r.log_record for r in exporter.get_finished_logs()]
    assert record.body == "connection to broker lost"
    assert record.severity_text == "WARN"


def test_an_exported_record_inside_a_span_is_linked_to_that_trace(
    exported: tuple[logging.Logger, InMemoryLogRecordExporter],
) -> None:
    logger, exporter = exported

    with tracer.start_as_current_span("work") as span:
        logger.info({"event": "inside_span"})
        expected_trace_id = span.get_span_context().trace_id

    (record,) = [r.log_record for r in exporter.get_finished_logs()]
    assert record.trace_id == expected_trace_id
