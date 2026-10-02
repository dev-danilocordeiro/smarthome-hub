from collections.abc import Sequence

import httpx
import pytest
from opentelemetry.sdk.metrics.export import (
    ExponentialHistogramDataPoint,
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

DataPoint = NumberDataPoint | HistogramDataPoint | ExponentialHistogramDataPoint

DEMO_ROUTE = "/diagnostics/trace-demo"


@pytest.fixture
def spans(span_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    span_exporter.clear()
    return span_exporter


def spans_of_trace(exporter: InMemorySpanExporter, trace_id: str) -> Sequence[ReadableSpan]:
    return [
        s
        for s in exporter.get_finished_spans()
        if s.context is not None and format(s.context.trace_id, "032x") == trace_id
    ]


def server_span(spans: Sequence[ReadableSpan]) -> ReadableSpan:
    (span,) = [s for s in spans if s.kind is SpanKind.SERVER]
    return span


async def test_one_request_produces_a_single_trace_spanning_http_redis_postgres_and_custom_work(
    client: httpx.AsyncClient, spans: InMemorySpanExporter
) -> None:
    response = await client.get(DEMO_ROUTE)

    assert response.status_code == 200
    trace_spans = spans_of_trace(spans, response.json()["trace_id"])
    root = server_span(trace_spans)
    assert root.name == f"GET {DEMO_ROUTE}"
    assert root.parent is None

    clients = [s for s in trace_spans if s.kind is SpanKind.CLIENT]
    assert {s.attributes.get("db.system.name") for s in clients if s.attributes} >= {
        "redis",
        "postgresql",
    }
    assert any(s.name == "diagnostics.simulated_work" for s in trace_spans)
    # Nothing in the trace is orphaned: every non-root span hangs off a span in the trace.
    span_ids = {s.context.span_id for s in trace_spans if s.context}
    assert all(s.parent and s.parent.span_id in span_ids for s in trace_spans if s is not root)


async def test_a_failed_request_marks_both_the_server_span_and_the_failing_step_as_errors(
    client: httpx.AsyncClient, spans: InMemorySpanExporter
) -> None:
    response = await client.get(DEMO_ROUTE, params={"fail": "true"})

    assert response.status_code == 500
    root = server_span(spans.get_finished_spans())
    (work,) = [s for s in spans.get_finished_spans() if s.name == "diagnostics.simulated_work"]
    assert root.status.status_code is StatusCode.ERROR
    assert work.status.status_code is StatusCode.ERROR
    assert work.context is not None
    assert root.context is not None
    assert work.context.trace_id == root.context.trace_id


async def test_health_probes_produce_no_spans_even_though_they_query_postgres_and_redis(
    client: httpx.AsyncClient, spans: InMemorySpanExporter
) -> None:
    assert (await client.get("/health/ready")).status_code == 200
    assert (await client.get("/health/live")).status_code == 200

    assert spans.get_finished_spans() == ()


def points(reader: InMemoryMetricReader, name: str) -> list[DataPoint]:
    data = reader.get_metrics_data()
    assert data is not None
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


async def test_request_latency_is_recorded_per_route_with_stable_http_conventions(
    client: httpx.AsyncClient, metric_reader: InMemoryMetricReader
) -> None:
    await client.get(DEMO_ROUTE)

    routes = {
        (p.attributes or {}).get("http.route")
        for p in points(metric_reader, "http.server.request.duration")
    }
    assert DEMO_ROUTE in routes


async def test_demo_runs_are_counted_by_outcome(
    client: httpx.AsyncClient, metric_reader: InMemoryMetricReader
) -> None:
    await client.get(DEMO_ROUTE)
    await client.get(DEMO_ROUTE, params={"fail": "true"})

    outcomes = {
        (p.attributes or {}).get("outcome")
        for p in points(metric_reader, "smarthome.diagnostics.demo.runs")
    }
    assert outcomes >= {"succeeded", "failed"}


async def test_the_diagnostics_endpoint_rejects_delays_beyond_its_cap(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(DEMO_ROUTE, params={"delay_ms": 60_000})

    assert response.status_code == 422
