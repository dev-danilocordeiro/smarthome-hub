# Architecture

How the hub fits together, from the outside in. Decisions and their trade-offs are in
the [ADRs](adr/); security in the [threat model](security/threat-model.md).

## Context

```mermaid
flowchart LR
  person(["Household member<br/>(owner, resident, viewer, guest)"])
  hub["Smart Home Hub"]
  devices(["Devices<br/>lights, plugs, lock, sensors,<br/>meter, camera (or the simulator)"])
  idp["Keycloak<br/>(identity)"]
  mail(["Email inbox"])
  hook(["Household's own tools<br/>(webhook receiver)"])
  ops(["Operator"])

  person -- "browser / PWA" --> hub
  person -- "signs in" --> idp
  hub -- "OIDC" --> idp
  devices <-- "MQTT 5 over TLS" --> hub
  hub -- "alerts" --> mail
  hub -- "signed alerts" --> hook
  ops -- "Grafana, alerts" --> hub
```

## Containers

One codebase (`apps/api/src/smarthome`), three entrypoints, each its own process and
image tag ([ADR 0001](adr/0001-modular-monolith-with-multiple-entrypoints.md)).

```mermaid
flowchart TB
  browser["Web app<br/>React + TS, PWA"]
  subgraph hub["Hub processes (same code)"]
    api["api<br/>FastAPI: BFF, REST, live WebSocket"]
    ingestor["ingestor<br/>MQTT consumer: telemetry, state,<br/>presence, acks"]
    worker["worker<br/>outbox relay, automations, energy rollup,<br/>alerts, notification delivery"]
  end
  broker["Mosquitto<br/>TLS, per-device ACLs"]
  kc["Keycloak"]
  pg[("PostgreSQL 16 + TimescaleDB<br/>one schema per module")]
  redis[("Redis<br/>sessions, live state,<br/>event stream, caches")]
  smtp["SMTP<br/>(Mailpit in development)"]

  browser -- "HTTPS + cookie, WebSocket" --> api
  api -- "OIDC" --> kc
  devices["Devices"] <-- "MQTT TLS" --> broker
  broker -- "shared subscriptions" --> ingestor
  worker -- "commands (QoS 1)" --> broker
  api & ingestor & worker --> pg
  api & ingestor & worker --> redis
  worker --> smtp
  worker -- "webhooks" --> receivers["Webhook receivers"]
```

Not drawn: every process sends traces, metrics and logs over OTLP to the OpenTelemetry
Collector, which feeds Prometheus, Tempo and Loki behind Grafana; Prometheus alert rules
go to Alertmanager ([ADR 0002](adr/0002-observability-pipeline.md)).

| Process | Does | Scales by |
|---|---|---|
| `api` | BFF (login, session, CSRF), REST for every module, one live WebSocket per open home | replicas (stateless; each tails the event stream) |
| `ingestor` | validates device messages, writes telemetry in batches, updates twins and presence, records command acks, publishes device events | replicas (MQTT shared subscriptions for telemetry and acks) |
| `worker` | publishes the command outbox, expires commands, runs automations (event stream consumer group, timers, schedules), energy rollup, alerts and their deliveries | replicas (consumer groups, `SKIP LOCKED`, advisory locks) |

## Modules

Each module owns a Postgres schema, exposes `public.py` and nothing else; the domain
layer imports no framework. Both rules are checked in CI (import-linter).

```mermaid
flowchart LR
  identity["identity<br/>homes, members, roles,<br/>sessions, audit"]
  devices["devices<br/>pairing, credentials,<br/>twin, presence"]
  telemetry["telemetry<br/>readings, aggregates"]
  commands["commands<br/>outbox, acks, timeouts"]
  automations["automations<br/>rules, scenes, schedules"]
  energy["energy<br/>hourly use, tariffs"]
  notifications["notifications<br/>alerts, inbox, deliveries"]

  telemetry --> devices
  devices -. "live view" .-> telemetry
  commands --> devices
  automations --> devices & telemetry & commands
  energy --> telemetry & devices
  notifications --> identity & devices & energy
```

An arrow means "reads through the other module's `public.py`". Every module's HTTP
layer also checks access through `identity` (not drawn). No foreign keys cross
schemas; the device event stream (`shared/events`) carries changes between them without
coupling producers to consumers.

## Where data lives

| Data | Store | Why there | ADR |
|---|---|---|---|
| Homes, members, invitations, audit log | Postgres `identity`, `audit` | transactional, hash-chained audit | 0004 |
| Devices, twins, pairing codes | Postgres `devices` | configuration of record | 0006 |
| Readings | TimescaleDB hypertable + 1 m / 1 h / 1 d continuous aggregates | time series, compression, retention | 0007 |
| Latest readings, presence, reported state | Redis | read on every screen, written on every message | 0008 |
| Commands, outbox | Postgres `commands`, `outbox` | written in the same transaction | 0009 |
| Device events | Redis stream `events:devices` | buffer between "changed" and "react" | 0010 |
| Automations, runs, scenes | Postgres `automations` | versioned configuration, run history | 0010 |
| Hourly energy, tariffs | Postgres `energy` | outlives raw telemetry, exact money | 0012 |
| Alerts, inbox, delivery queue | Postgres `notifications` | uniqueness and transactions decide | 0013 |
| BFF sessions (with OAuth tokens) | Redis, keyed by hashed session id | short-lived, shared by API replicas | 0003 |

## Flows

### A reading, from device to screen

```mermaid
sequenceDiagram
  participant D as Device
  participant B as Broker
  participant I as ingestor
  participant R as Redis
  participant P as TimescaleDB
  participant A as api (live socket)
  participant W as Web app
  D->>B: telemetry (QoS 0, TLS)
  B->>I: $share/ingestor/... telemetry
  I->>I: schema, active device, flood guard, dedup
  I->>R: latest readings
  I->>I: write buffer (500 rows or 1 s)
  I->>P: INSERT ... unnest(...) ON CONFLICT DO NOTHING
  I->>R: XADD events:devices
  R-->>A: XREAD (every API replica)
  A-->>W: {"type": "telemetry", ...}
```

### A command, from click to light

```mermaid
sequenceDiagram
  participant W as Web app
  participant A as api
  participant P as Postgres
  participant K as worker (relay)
  participant B as Broker
  participant D as Device
  participant I as ingestor
  W->>A: POST /commands {"on": true}
  A->>P: command + outbox row (one transaction)
  A-->>W: 202 {id, status: pending}
  P-->>K: NOTIFY outbox
  K->>B: PUBLISH commands (QoS 1, traceparent)
  B->>D: command
  D->>B: ack "received", then "applied" + new state
  B->>I: acks, state
  I->>P: command acknowledged, twin reported
  I-->>W: command + state events over the live socket
```

One trace covers every hop, including the device (W3C trace context in MQTT 5 user
properties, [ADR 0009](adr/0009-commands-outbox-and-trace-propagation.md)).

### An automation

```mermaid
sequenceDiagram
  participant I as ingestor
  participant S as events:devices
  participant E as worker (engine)
  participant P as Postgres
  participant C as commands
  I->>S: motion = true
  S->>E: XREADGROUP automations
  E->>P: lock trigger state, edge false → true?
  E->>P: run recorded (unique per automation and event)
  E->>C: issue command (actor automation:{id})
  E->>S: XACK
```

### An alert

```mermaid
sequenceDiagram
  participant S as events:devices
  participant E as worker (alerts)
  participant P as Postgres
  participant Q as worker (dispatcher)
  participant M as SMTP / webhook
  S->>E: presence offline (lock)
  E->>P: INSERT alert pending, due in 5 min (one live per key)
  Note over E: timer: due and still offline?
  E->>P: open + inbox entries + queued deliveries (one transaction)
  E->>S: alert event → live sockets → bell
  Q->>P: claim due deliveries (SKIP LOCKED)
  Q->>M: email / signed POST
  Q->>P: sent, or retry with backoff
```

### Signing in (BFF)

```mermaid
sequenceDiagram
  participant W as Browser
  participant A as api (BFF)
  participant K as Keycloak
  participant R as Redis
  W->>A: GET /auth/login
  A-->>W: 302 to Keycloak (PKCE S256, state, nonce) + login cookie
  W->>K: credentials or passkey
  K-->>W: 302 /auth/callback?code
  W->>A: callback
  A->>K: code + verifier (confidential client)
  K-->>A: ID, access, refresh tokens
  A->>R: session (tokens stay here)
  A-->>W: __Host- session cookie (HttpOnly, SameSite=Strict)
```
