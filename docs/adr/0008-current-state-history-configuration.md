---
status: accepted
date: 2026-10-02
---

# 0008. Three stores for three questions: current state, history, configuration

## Context and problem statement

The hub answers three very different kinds of question:

- **What is the house doing right now?** Every dashboard tile, every WebSocket push,
  every automation condition. This means high read rates, a single value per key, and
  tolerance for a few seconds of loss on a crash.
- **What happened over time?** Charts, energy reports, dry runs of automations against
  history. This means append-heavy writes, range scans, and aggregates.
- **What exists and who may do what?** Homes, members, devices, pairing, twins'
  desired state, automations. This means relational data, invariants and audit, where
  correctness beats speed.

One database serving all three would be either too slow for the first or too loose for
the third.

## Decision outcome

| Question       | Store                                 | Written by                    | Read by |
|----------------|---------------------------------------|-------------------------------|---------|
| Current state  | **Redis** hashes `device:{id}` (presence, reported twin) and `device:{id}:readings` (last value per metric) | ingestor (devices, telemetry) | dashboards, WebSocket (phase 9), automations (phase 8) |
| History        | **TimescaleDB** hypertable + continuous aggregates (ADR 0007) | ingestor (batched) | charts, energy, automation dry-run |
| Configuration  | **PostgreSQL** schemas per module (identity, devices, …), constraints and the audit log | API | everything else |

### Rules

- **Dashboards never query TimescaleDB for "now".** Latest values come from Redis in
  O(1); history endpoints serve charts only, and even those mostly hit aggregates.
- **Redis is a cache of facts owned elsewhere, not a source of truth.**
  - Presence and twins are durable in Postgres; Redis mirrors them for speed.
  - Last readings can always be rebuilt from TimescaleDB.
  - Losing Redis degrades freshness until the next message arrives (presence and state
    are retained at the broker and replayed on the ingestor's next subscribe). It
    loses nothing permanent.
- **Writes to Redis are guarded by event time** (a Lua compare-and-set on per-field
  timestamps), so a delayed message cannot roll the live view back.
- Postgres and TimescaleDB live in the **same server** today, as different schemas
  with very different access patterns. They can be split onto separate instances
  without code changes, since each module reaches its own schema only through its
  adapters.

### Consequences

- Good: each workload gets the right tool. Redis serves the hot path at sub-millisecond
  speed, Timescale handles volume and time, Postgres handles invariants.
- Good: failure isolation. A Redis restart costs a few seconds of staleness; a slow
  analytical query cannot slow down a login.
- Bad: three things to run, and two copies of current state (Postgres plus Redis) that
  must agree. The ingestor writes both from the same message; Postgres wins on
  conflict and Redis converges on the next message.
