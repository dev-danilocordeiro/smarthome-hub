---
status: accepted
date: 2026-10-03
---

# 0015. Testing strategy: real infrastructure, proven concurrency, a browser at the end

## Context and problem statement

The hub's hardest guarantees live where code meets infrastructure: a unique index that
deduplicates alerts, `FOR UPDATE SKIP LOCKED` in the outbox and delivery queues, advisory
locks, continuous aggregates, MQTT ACLs, a Keycloak login, a WebSocket closed with the
right code. Tests that mock those away pass while the product is broken. At the same
time, a suite that needs a full stack for every assertion is too slow to run on every
change.

## Decision drivers

- A green suite must mean the guarantee holds, not that a mock agreed.
- Fast feedback for domain rules; real dependencies where behaviour depends on them.
- Every concurrency guarantee is demonstrated, not assumed.
- The whole system is exercised the way a person uses it at least once per change.

## Decision outcome

Five layers, each with one job:

| Layer | What | Tools | Runs |
|---|---|---|---|
| **Unit** | domain rules and application services; ports replaced by small in-memory fakes, never by mocks of SQL or HTTP | pytest, Hypothesis (protocol and DSL properties); Vitest + Testing Library for the web app (routed `fetch`, fake WebSocket) | every PR, seconds |
| **Integration** | each adapter against the real thing: TimescaleDB, Redis, Mosquitto with TLS and dynamic security, Keycloak, Mailpit, a local webhook receiver | Testcontainers; the API through `httpx.ASGITransport`, live sockets over real uvicorn | every PR, ~2-4 min |
| **Static contracts** | module boundaries, OpenAPI and TS types in sync, compose/collector/Prometheus/Loki/Tempo configs, dashboards, alert rules (`promtool test rules`), workflows | import-linter, mypy `--strict`, tsc, promtool, amtool, actionlint | every PR |
| **End to end** | a real browser through Keycloak, the BFF, the broker, a simulated fleet and the live socket | Playwright against `make up` + `make simulate FAULTS=0` + Vite | every PR (`e2e.yml`), ~1 min of tests |
| **Load** | throughput, latency and the first saturation point | the simulator at a multiple of its rate, k6, a Prometheus report | on demand ([results](../load-test.md)) |

Rules that apply across layers:

- **No substitute databases.** No SQLite or in-memory stand-ins for Postgres or Redis:
  partial unique indexes, `SKIP LOCKED`, advisory locks, hypertables and streams do not
  exist there, so a green run would prove nothing.
- **Concurrency tests must fail without the guarantee.** Each lock or uniqueness rule
  has a test that races it (concurrent `asyncio.gather`, a held transaction, a slowed
  read that forces interleaving), and the test is checked once with the guarantee
  removed. Examples: last-owner protection, single-use invitations and pairing codes,
  session refresh, automation cooldown, the first tariff save, alert deduplication.
- **Test names are sentences about behaviour**
  (`test_an_event_older_than_the_alert_cannot_resolve_it`).
- **Time is injected.** Services take a `Clock`; tests move it instead of sleeping.
- **E2E covers journeys, not permutations.** Sign-in, live control of a device, energy,
  alerts, sign-out and one tenant-isolation check. Rules and edge cases belong to the
  layers below, where they are cheap.

### Consequences

- Good, because failures found by the E2E and load tests were real: browsers report a
  WebSocket refused before the handshake as code 1006 whatever the server meant (now
  refused after accepting, with 4401/4403); histograms in seconds used millisecond
  buckets (every quantile read ~5 s).
- Good, because integration tests run the same images as `make up`.
- Bad, because the integration and E2E layers need Docker and a few minutes; local
  iteration uses the unit layer and targeted integration modules.
- Bad, because the E2E job starts the whole stack on every PR (~10 minutes of CI time).

### Confirmation

`make check`, `make check-infra`, `make test`, `make e2e`; the CI workflows `backend`,
`web`, `infra`, `security` and `e2e`.
