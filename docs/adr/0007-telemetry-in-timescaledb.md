---
status: accepted
date: 2026-10-02
---

# 0007. Telemetry in TimescaleDB: long format, hierarchical aggregates, batched ingestion

## Context and problem statement

Every device reports readings every 5–120 s. A modest fleet (thousands of devices)
produces millions of rows a day. Dashboards need "last 2 hours, every sample" and also
"last year, daily". Old raw data has little value; old aggregates have a lot. Writes
must keep up with bursts, never lose data on a database hiccup, and never take the
ingestor down by buffering without bound.

## Decision outcome

### Storage

- **One hypertable**, `telemetry.readings`, partitioned by time in 1-day chunks.
  - **Long format**: `(time, home_id, device_id, metric, value, message_id, received_at)`.
    Kinds report different metrics. A wide table would be mostly NULLs and need a
    migration per new metric; here a new metric is just a new value of `metric`.
  - Booleans (motion, contact) are stored as 1.0/0.0, so their average is "share of
    time true".
  - `home_id` is denormalised, so per-home queries never join across modules.
  - `UNIQUE (device_id, metric, time)` is the database-level dedup guard (it must
    include the partitioning column).
- **Columnstore** (TimescaleDB ≥ 2.18's name for compression) after 7 days. It is
  segmented by `device_id, metric` and ordered by `time DESC`, which matches the only
  read pattern: one series, one time range.
- **Hierarchical continuous aggregates**: `readings_1m` → `readings_1h` → `readings_1d`.
  - Each level stores `total` and `samples` (plus min, max, last), never `avg`, so a
    coarser level is computed exactly from the finer one; averaging averages would be
    wrong for uneven sample counts.
  - The integration test checks that the hourly totals equal the raw sum.
  - All levels use **real-time aggregation** (`materialized_only = false`): the newest,
    not-yet-materialised window is computed from raw rows at query time, so charts have
    no "last minute missing" gap.
  - Refresh policies: 1m every minute over the last 2 h; 1h every 15 min over 3 days;
    1d hourly over 35 days.
- **Retention is configuration, not schema**: raw 30 days, 1m 90 days, 1h 2 years, 1d
  forever (`SMARTHOME_TELEMETRY_*_RETENTION_DAYS`). The ingestor applies it idempotently
  at startup, so a change ships as a config change plus a restart, with no migration.

### Reads

`GET /homes/{h}/devices/{d}/telemetry?metric&from&to[&resolution]` chooses a resolution
from the range when none is given:

| Range    | Up to 2 h | Up to 1 day | Up to 60 days | Beyond |
|----------|-----------|-------------|---------------|--------|
| Source   | raw       | `readings_1m` | `readings_1h` | `readings_1d` |

That gives a few hundred to ~1500 points. Closed ranges (with `to`) are cached in Redis
for 10 s; open-ended ranges ("until now") are never cached.
`GET .../readings/latest` reads Redis only (ADR 0008).

### Ingestion

The telemetry handler runs in the ingestor on an MQTT 5 **shared subscription**, so
replicas split the load. For every message it does, in order:

1. **Active device?** The answer comes from `devices.public`, cached in process for 30 s.
   A device that was just quarantined or revoked is already disconnected by the broker.
2. **Flood guard.** A Redis per-minute counter per device. Past the limit (default 120
   messages/min; the busiest simulated device sends 12), the device is **quarantined
   once** through `devices.public` (broker disable plus audit, actor `system:ingestor`)
   and its traffic is dropped.
3. **Dedup** by `message_id`, using Redis `SET NX EX 600`. The database unique key
   catches whatever slips through.
4. Latest values go to Redis, and the rows go into the **bounded write buffer**.

The buffer:

- **Flushes** when 500 rows are waiting or every second, whichever comes first. One
  statement per batch: `INSERT … SELECT * FROM unnest(arrays) ON CONFLICT DO NOTHING`.
- Applies **backpressure**: when 20 000 rows are waiting, `put()` blocks. That stalls
  the MQTT consumer, and the broker absorbs the slack (or drops QoS 0 telemetry), rather
  than the ingestor growing until it is OOM-killed.
- **Retries** a failed flush with the rows kept, so a database hiccup costs latency,
  not data.
- **Shuts down cleanly**: the consumer stops first, then the buffer drains, and the
  flush loop wakes on stop. A test caught that it used to wait out a full interval.
- **Metrics**: buffer size, batch size, flush duration and failures, rows written vs
  duplicate, and **ingestion latency** (device timestamp → durable write, sampled per
  batch).

### Consequences

- Good: a dashboard query never scans more than ~1500 rows, whatever the range.
- Good: storage decays gracefully. Raw rows are compressed after a week and dropped
  after a month; aggregates live on.
- Bad: long format costs more rows than a wide table (one per metric). Columnstore
  compression on `(device_id, metric)` segments recovers most of it.
- Bad: the 30 s directory cache means up to 30 s of telemetry could be accepted from a
  device revoked at the database while still connected. In practice revocation kicks
  the device at the broker first (ADR 0006), so nothing arrives.
