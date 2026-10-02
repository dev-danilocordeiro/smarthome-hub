---
status: accepted
date: 2026-10-02
---

# 0002. Observability pipeline: OTLP to one Collector, then Prometheus, Tempo and Loki

## Context and problem statement

The core flows of this system cross process and protocol boundaries:
browser → API → MQTT → device → ack → WebSocket, and telemetry → ingestor →
TimescaleDB → automation → command. When one of them is slow or broken we need to
answer *where* within minutes, starting from any signal: a latency spike, an error
log or a single user report.

How should services emit telemetry, and how does it reach storage and dashboards?

## Decision drivers

- One instrumentation API for traces, metrics and logs in every process (api,
  ingestor, worker, simulator, web).
- Being able to jump from a metric to a trace to the logs of that trace, in both
  directions.
- No vendor lock-in in application code: swapping a backend must be a config change.
- Configs that can be validated in CI, and dashboards kept as code.

## Considered options

1. **Prometheus client libraries + stdout logs scraped by an agent + a tracing SDK**
   (one tool per signal).
2. **OpenTelemetry SDK, OTLP to an OpenTelemetry Collector**, which fans out to
   Prometheus, Tempo and Loki through each backend's native OTLP endpoint.
3. **OpenTelemetry SDK straight to each backend** (no Collector).

## Decision outcome

Chosen option: **2.**

```
services ──OTLP/gRPC──▶ otel-collector ──▶ Tempo        (traces)
                                       ──▶ Prometheus   (metrics, /api/v1/otlp)
                                       ──▶ Loki         (logs, /otlp)
Tempo metrics-generator ──remote write──▶ Prometheus (service graph, span metrics)
Grafana ◀── all three, with cross-links provisioned
```

### Details that make the correlation work

- **Resource attributes** (`service.name`, `service.namespace`, `service.version`,
  `service.instance.id`, `deployment.environment.name`) are set once per process.
  Prometheus promotes them to labels and Loki indexes them, so every signal filters
  by the same keys.
- **Stable semantic conventions** (`OTEL_SEMCONV_STABILITY_OPT_IN=http,database`):
  `http.server.request.duration` in seconds with `http.route`, which the RED
  dashboard and the future SLOs are built on.
- **Exemplars**: the SDK attaches the current trace id to histogram samples.
  Prometheus stores them (`exemplar-storage`), and Grafana links each one to Tempo.
- **Logs**: structlog stays the logging API. Every record is (a) rendered as JSON on
  stdout with `trace_id`/`span_id`, for `docker logs` and for any log shipper, and
  (b) exported over OTLP by `OtlpLogHandler`. The structlog `event` becomes the OTLP
  body and the other keys become attributes, which Loki keeps as structured
  metadata. Loki's `trace_id` then links to Tempo, and Tempo's "logs for this span"
  queries Loki by `trace_id`.
- **Noise control**: `/health/*` is excluded from HTTP instrumentation, and readiness
  probes run under `suppress_instrumentation`. Otherwise a 10-second healthcheck
  would produce more traces than real traffic and skew RED metrics.
- **Sampling**: `ParentBased(TraceIdRatioBased(ratio))`. The ratio is 1.0 locally and
  configurable through `SMARTHOME_OTEL_TRACES_SAMPLE_RATIO`. Parent-based sampling
  keeps a trace whole across processes (and, from phase 7, across MQTT).
- **Opt-in at runtime**: `SMARTHOME_OTEL_ENABLED` defaults to false. The base compose
  file runs without the observability stack; `docker-compose.observability.yml`
  turns it on. Instrumentation is always applied, and without providers the OTel API
  is a no-op, so code paths do not change.

### Consequences

- Good: changing a backend (e.g. Tempo for Jaeger, Loki for Elasticsearch) touches
  only `infra/otel-collector/config.yaml`.
- Good: one Grafana click goes from a p99 spike (exemplar) to the trace, and from
  any span to its logs.
- Good: the Collector is the single place for future cross-cutting concerns:
  tail sampling, PII scrubbing, routing per tenant.
- Bad: one more moving part. A Collector outage loses telemetry (the SDKs retry with
  bounded queues; stdout logs survive). Acceptable for this project; production
  would run it as an agent plus a gateway.
- Bad: the OTel Logs API in Python is still `_logs` (experimental). The bridge is
  ~50 lines with unit tests, so it is cheap to replace.
- Bad: `opentelemetry-instrumentation-sqlalchemy` lags SQLAlchemy releases (it skips
  2.1 silently). SQLAlchemy is pinned to `<2.1`. The integration test that asserts
  Postgres spans exist will flag the next mismatch.

### Confirmation

- `tests/integration/test_observability.py`: one request yields one trace with HTTP,
  Redis, Postgres and custom spans and no orphans; failures mark spans as errors;
  health probes emit nothing; latency is recorded per route with stable conventions.
- `tests/unit/test_logging.py`: log ↔ trace correlation and the OTLP log mapping.
- `make check-infra` (CI workflow `infra`) validates the compose files, the Collector,
  Prometheus, Tempo and Loki configs with each tool's own validator, and checks that
  every dashboard references only provisioned datasources.

### Deferred

- Trace context over MQTT 5 user properties: ADR with phase 7 (commands).
- SLOs and alerting rules: ADR with phase 10.
- Browser (OpenTelemetry Web) traces: phase 9. The Collector already accepts
  OTLP/HTTP with CORS for the dev origin.

## Pros and cons of the options

### 1. One tool per signal

- Good: each tool is mature in isolation.
- Bad: three APIs in the code, and correlation has to be hand-built (trace ids
  copied into logs, no exemplars without extra work).

### 3. SDK straight to each backend

- Good: no Collector to run.
- Bad: every service knows every backend's address and protocol. Batching, retries
  and scrubbing are duplicated per service, and swapping a backend means redeploying
  every service.
