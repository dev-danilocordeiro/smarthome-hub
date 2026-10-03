# Load test

What one copy of the stack handles on a developer machine, where it saturates first, and
how to reproduce the numbers. Measured on 2026-10-03.

## Setup

| | |
|---|---|
| Host | AMD Ryzen 7 5700X (8 cores / 16 threads), 32 GB RAM, NVMe, Linux |
| Stack | `make up`: every service in Docker on the same host, one replica each, observability on |
| Fleet | 50 homes × 20 devices = **1,000 devices**, each with its own MQTT 5 TLS connection and credentials, paired through the real provisioning endpoint |
| Traffic | the simulator's normal telemetry, sent `--rate 5` and `--rate 8` times as often; faults off |
| Readers | k6 2.3.0, 20 virtual users signed in as one member (real Keycloak login through the BFF), no think time |

The load generator runs on the same host as the stack, so every figure includes the
contention of a single machine running Postgres, Redis, Mosquitto, Keycloak, the
observability stack, 1,000 TLS clients and k6 at once.

## Results

### Telemetry ingestion

| | rate ×5 | rate ×8 |
|---|---:|---:|
| Device messages accepted | 235 msg/s | 376 msg/s |
| Rows written to TimescaleDB | 846 rows/s | 1,353 rows/s |
| Ingest latency p50 (device timestamp → committed row) | 79 ms | 45 ms |
| Ingest latency p95 | 643 ms | 434 ms |
| Ingest latency p99 | 729 ms | 487 ms |
| Handler time per MQTT message, p95 | < 5 ms | < 5 ms |
| Batch insert, p95 | 25 ms | 25 ms |
| Average batch | 262 rows | 266 rows |
| Write buffer, max | 511 rows | 528 rows |
| Device event stream lag p95 (automations, alerts) | < 5 ms | < 5 ms |
| Ingestor CPU | — | 66 % of one core |
| Ingestor memory | 145 MiB | 150 MiB |

Nothing was dropped, quarantined or rejected. Latency is dominated by the write buffer's
flush interval (1 s or 500 rows, whichever comes first): at the higher rate batches fill
sooner, so latency **drops**. The write buffer never came near its back-pressure limit
(20,000 rows).

### HTTP reads, during ingestion at ×5

| Endpoint | p50 | p95 | p99 |
|---|---:|---:|---:|
| `GET /homes/{h}/devices` | 75 ms | 116 ms | 188 ms |
| `GET …/readings/latest` (Redis) | 82 ms | 155 ms | 191 ms |
| `GET …/telemetry?metric&from` (a day, 1-minute aggregate) | 107 ms | 184 ms | 214 ms |
| `GET /homes/{h}/energy/usage` (a day, hourly) | 101 ms | 170 ms | 207 ms |
| `GET /me/notifications/unread-count` | 72 ms | 104 ms | 173 ms |
| **All** | **88 ms** | **150 ms** | **202 ms** |

**216 requests/s, 0 errors** (25,915 requests in 2 minutes). Every k6 threshold passed
(p95 < 300 ms; < 500 ms for history and energy). Meanwhile ingestion kept up (266 msg/s),
with its p95 latency rising from 0.6 s to 1.3 s.

## Where it saturates

- **The API process is the first limit for reads.** During the k6 run it used 100 % of
  one core: one uvicorn process at about 216 req/s. Every request resolves the session
  (Redis) and the membership (Postgres) before doing its own work. The API is stateless
  (sessions in Redis, live fan-out from the event stream, ADR 0011), so the next step is
  more processes or replicas, not a different design.
- **The ingestor is the next limit for writes**: about 66 % of one core at 376 msg/s,
  so roughly 550 msg/s per replica. Telemetry and acks use MQTT shared subscriptions
  (ADR 0006/0007), so a second replica splits that traffic without code changes.
- Postgres, Redis and Mosquitto stayed far from saturation: batch inserts took 25 ms at
  p95 at 1,350 rows/s.

## What the test found

Running the load test changed the code:

1. **Every duration histogram reported about 5 s.** The OpenTelemetry SDK's default
   bucket boundaries (0, 5, 10, 25 … 10,000) are meant for milliseconds; ours record
   seconds, so nearly every sample fell into the first bucket and every quantile
   interpolated to 4.75 s. The first run's report showed that same figure for handler
   time, flush duration, ingest latency and stream lag, which gave it away. A
   metrics `View` now gives every `unit="s"` histogram seconds-scale boundaries (5 ms to
   1 h); a unit test pins it. Dashboards and alert rules on those histograms only became
   meaningful with this fix.
2. **The alert engine looked up the device for every battery reading.** Most readings
   can only *clear* an alert (a healthy battery, a device coming back online), and
   clearing needs neither the device's name nor its kind. Clears now go straight to one
   guarded `UPDATE`.
3. Process CPU and memory were only reported by the API; the ingestor and worker now
   report them too.

## Reproduce

```bash
make up
make loadtest-ingest                                # pairs the fleet on first run (owner: dave), 5 min at x5, prints a report
make loadtest-ingest LOAD_RATE=8 LOAD_SECONDS=240
make web-dev                                        # in another terminal: the login goes through the BFF
make loadtest-api VUS=20 SECONDS=120                # k6; summary in .loadtest/api-summary.json
make loadtest-reset                                 # forget the fleet (its homes stay; `make nuke` clears everything)
```

The report (`scripts/loadtest/report.py`) reads Prometheus over the test window only.
Keep `LOAD_RATE` at 8 or below: the whole-home meter reports every 5 s, and above that
rate it crosses the per-device flood limit (120 messages/minute) and gets quarantined,
which is the guard doing its job.
