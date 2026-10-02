---
status: accepted
date: 2026-10-02
---

# 0009. Device commands: transactional outbox, ack state machine, traces over MQTT 5

## Context and problem statement

A resident taps "unlock" in the app. The hub has to record what was asked and by whom,
get the command to a device that may be offline for a moment, learn whether the device
actually did it, and give up cleanly if it never answers. Two systems are involved, the
database and the MQTT broker, and they cannot share a transaction:

- publish first, then commit: if the commit fails the device acts on a command the hub
  has no record of (and a lock opens with nothing in the audit log);
- commit first, then publish from the request: if the process dies in between, or the
  broker is briefly down, the command is recorded but never sent, and the request
  handler has to wait on the broker.

On top of that, an operator debugging "the light did not turn on" needs to follow one
command across the API, whatever publishes it, the broker, the device and the ack.

## Decision drivers

- A command is sent if and only if it was recorded (and, for locks and cameras, audited).
- The HTTP request never waits on the broker.
- At-least-once delivery is fine if every hop is idempotent; exactly-once is not on offer.
- A late or duplicated ack must never rewrite a command's outcome.
- One trace from the request to the device's ack.

## Considered options

1. Publish from the request handler after commit (best effort).
2. **Transactional outbox** in Postgres, relayed by the worker.
3. A job queue (arq on Redis) enqueued after commit.
4. Change data capture (Debezium / logical decoding) into a message bus.

## Decision outcome

Chosen option: **2, a transactional outbox relayed by the worker**, because it is the only
one that makes "recorded" and "sent" the same fact without a second piece of
infrastructure. Option 3 has the same dual-write gap as option 1 (enqueue after commit
can fail); option 4 is the industrial version of option 2 and far more than a hub needs.

### How it works

```
POST /commands ──► tx { commands.commands row, outbox.messages row, audit (critical) } ──► 202
                                   │ NOTIFY outbox (on commit)
worker: OutboxRelay ◄──────────────┘  (+ 1 s poll fallback)
  SELECT … FOR UPDATE SKIP LOCKED  ─► publish QoS 1, await PUBACK ─► mark published
device ─► commands/ack (QoS 1) ─► ingestor ($share) ─► CommandsService.record_ack
worker: sweeper every 5 s ─► pending/delivered past expires_at + grace ─► timed_out
```

- **Outbox in the shared kernel** (`smarthome.shared.outbox`, schema `outbox`): any module
  appends through the `Outbox` port inside its own unit of work. Rows carry the MQTT
  topic, QoS, payload, headers (trace context) and an optional deadline.
- **Relay** (`OutboxRelay`, in the worker): claims due rows oldest first with
  `FOR UPDATE SKIP LOCKED`, publishes each at QoS 1 and marks it only after the broker's
  PUBACK, all inside the claiming transaction. Several workers can run side by side; a
  crash between PUBACK and commit republishes the row (devices deduplicate by
  `command_id`). A failed publish records the error, backs off exponentially (max 60 s)
  and stops the batch, so order is kept while the broker is down. A row whose deadline
  passed before it could be published is marked `expired` and dropped: the device would
  have to reject it anyway. Processed rows are purged after 24 h.
- **Wake-up:** a statement trigger sends `NOTIFY outbox`, which Postgres delivers on
  commit, so the relay usually publishes within milliseconds; polling covers lost
  notifications and a dropped LISTEN connection.
- **No job queue (arq).** The relay is one long-running loop and the sweeper one periodic
  `UPDATE`; plain asyncio tasks in the worker are enough and keep Redis out of the
  delivery path.

### Command lifecycle

| status         | meaning                                                         |
|----------------|-----------------------------------------------------------------|
| `pending`      | recorded; in the outbox or on its way                           |
| `delivered`    | the device acked `received`                                     |
| `acknowledged` | the device acked `applied` (final)                              |
| `failed`       | the device acked `rejected` or `failed`, with its reason (final)|
| `timed_out`    | acked `expired`, or no outcome by `expires_at` + 10 s (final)   |

Status only moves forward. An `applied` overtaking `received` still records delivery. A
settled command never changes again: in the domain (`Command.with_ack` returns nothing),
in the service (the row is locked, and the sweeper skips locked rows) and in the schema
(trigger `commands.guard_transition` rejects any update to a settled command, any change
to what was asked, and deletes). The race "ack vs sweeper" has a test that fails without
the locks.

The deadline (`ttl_s`, 5–300 s, default 30) is part of the message: devices refuse
commands that arrive late, so a command queued for an offline device cannot fire hours
later. The hub's grace period after the deadline gives an in-flight ack time to arrive.

### Twin desired state

`set_state` also merges `desired` into the device twin, through `devices.public`, **after**
the command's transaction commits. Modules do not share transactions (ADR 0001), so if
that second write fails the command still goes out and the twin catches up with the next
command; it is logged. Writes are ordered by `issued_at`, so concurrent commands cannot
roll the twin back.

Relays running side by side, and retries, can deliver two commands to the same device
out of order. Devices therefore ignore a `set_state` issued before the last one they
applied (`rejected: superseded by a newer command`), which the simulator implements and
the protocol documents.

### Authorization

| command                         | needs                                                  |
|---------------------------------|--------------------------------------------------------|
| anything                        | `control_devices` (a guest only within its device scope) |
| to a lock or camera             | also `operate_locks` and a sign-in within 5 minutes    |
| `reboot`                        | also `manage_devices`                                  |

The domain decides *which capabilities* a command needs (`requirements(kind, action)`);
the API maps them to identity permissions. A stale sign-in gets
`401 urn:smarthome:problem:reauthentication-required` with a `login_url` that forces a
fresh login at Keycloak (`prompt=login`, `max_age=0`). Commands to locks and cameras are
audited when issued and when settled.

### Trace propagation over MQTT 5

1. The outbox writer stores the request's W3C context in the row's headers.
2. The relay starts a `PRODUCER` span "outbox publish" as a child of it and sends *its*
   context as MQTT 5 user properties (`traceparent`, `tracestate`).
3. The device continues the trace ("device handle command" in the simulator, exported
   when `OTEL_EXPORTER_OTLP_ENDPOINT` is set) and puts its context on the acks.
4. The ingestor's consumer extracts user properties for every message, so each ack's
   "ingest device message" span joins the trace.

Payloads stay free of tracing fields. The command's `trace_id` is stored and returned by
the API, so "what happened to command X" is one click in Tempo.

### Consequences

- Good, because a command exists in the database if and only if it is (or will be) sent,
  and the request returns in milliseconds whatever the broker is doing.
- Good, because the broker can be down for minutes without losing a command; commands
  that outlive their deadline are dropped instead of firing late.
- Good, because one trace shows API, worker, device and ingestor (verified live: four
  services in one Tempo trace).
- Bad, because delivery is at-least-once: devices must deduplicate by `command_id`
  (protocol v1 already requires it).
- Bad, because the twin's desired state is written in a second transaction and can lag
  behind if that write fails.
- Bad, because two relays may publish to one device out of order; the device-side
  "superseded" rule covers `set_state`, the only action where order matters.

### Confirmation

- `tests/integration/commands`: outbox rollback, order, side-by-side relays never
  publishing twice (fails without `SKIP LOCKED`), broker outage with backoff, expiry,
  NOTIFY wake-up, purge, schema guard, ack vs sweeper race (fails without the locks),
  and an end-to-end command over real Mosquitto asserting one trace.
- Metrics: `smarthome.commands.issued`, `smarthome.commands.settled`,
  `smarthome.commands.outcome.latency`, `smarthome.outbox.relayed`,
  `smarthome.outbox.relay.lag`, `smarthome.outbox.backlog` (worker only).
