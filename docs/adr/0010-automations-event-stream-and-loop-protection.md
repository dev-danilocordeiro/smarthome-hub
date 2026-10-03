---
status: accepted
date: 2026-10-02
---

# 0010. Automations: device event stream, edge-triggered rules, loop protection

## Context and problem statement

"Turn the hall light on when there is motion after sunset, and off again after two
quiet minutes." "Lock the door at 23:00 on weekdays." An automation reacts to devices
(a reading, a twin property, presence) or to the clock, checks conditions, and sends
commands. The hub needs to answer several questions:

- How does the code that runs automations hear about device changes, when those
  changes are applied by the ingestor and automations should run in the worker?
- When exactly does "temperature above 26" fire: on every reading above 26, or once
  when it crosses? What about "for two minutes"?
- What stops two automations from switching a light on and off forever?
- How are automations edited safely (concurrent edits, history) and validated, and how
  can someone see when an automation *would* have run before trusting it?
- Who may create an automation that unlocks a door?

## Decision drivers

- Automations run in the background (worker), never in the request path, and never in
  the ingestor's hot path: a slow rule must not delay telemetry.
- Several worker replicas can run side by side without running an automation twice.
- Firing is defined precisely enough to test, and the dry run uses the same rules.
- A runaway automation stops itself, and a human is told why.
- An automation never grants more than its author is allowed to do.

## Considered options

For the event path:

1. Evaluate rules inside the ingestor, right after each change is applied.
2. **A Redis stream of device events**, written by the ingestor, read by a consumer
   group in the worker.
3. Publish events through the transactional outbox to an internal MQTT topic.
4. Poll Postgres (twins, readings) from the worker.

## Decision outcome

Chosen option: **2, a Redis stream with a consumer group**. Option 1 couples ingestion
latency to rule evaluation and, because presence and state are consumed by every
ingestor replica (ADR 0006), would need its own deduplication. Option 3 makes the
broker carry the hub's internal traffic and needs extra ACLs and a second consumer.
Option 4 cannot see individual telemetry messages (only rows after a flush) and adds
load proportional to the polling rate.

### Events

```
ingestor: devices / telemetry apply a change ──► XADD events:devices (MAXLEN ~100k)
worker:   XREADGROUP automations ──► AutomationEngine.handle_event ──► XACK
```

- `smarthome.shared.events` defines `DeviceEvent(home, device, kind, at, data)` for
  `state` (the full reported state), `presence` (`online`) and `telemetry` (one
  message's readings) and the `EventPublisher` port. Devices and telemetry publish
  **only when the change was applied**: a duplicate or stale message produces no event,
  so ingestor replicas that all see the same retained state message emit it once.
- Publishing happens **after** the change commits and is best effort. A crash between
  the commit and the `XADD` loses the reaction to that change, not the change. That is
  the accepted gap: the outbox (ADR 0009) would close it at the cost of a Postgres write
  per telemetry message, which is not worth it for a home.
- The consumer acknowledges an entry only after the engine returns. On start, and after
  any failure, it first re-reads its own pending entries; entries left pending by a dead
  worker are claimed (`XAUTOCLAIM`) after 60 s; an entry that fails five times, or
  cannot be decoded, goes to `events:devices:dead`. The group starts at `$`: events from
  before the first worker started are not replayed.
- The producer's trace context travels in the entry, so one trace shows the sensor
  message, the automation run, the command, the device and its acks.

### The DSL (v1)

A JSON document validated by a JSON Schema (2020-12) that the API serves at
`GET /automations/schema` for editors, plus rules a schema cannot express (ordering
operators compare numbers only, `set_state` needs `desired`, every referenced device,
property, metric and desired value must fit the device's kind in protocol v1). Errors
carry a JSON path (`triggers/0/value`). See [docs/automations.md](../automations.md).

- **Triggers** (any of them): `device_state`, `telemetry`, `presence` (each with an
  optional `for_s` hold) and `schedule` (wall-clock time and weekdays).
- **Conditions** (all of them): `time` windows (may wrap midnight), `device_state`,
  `presence`, `telemetry` (latest reading, `max_age_s`).
- **Actions** (in order): `command` (`set_state` or `identify`) and `scene`.
- `cooldown_s`. No delays or branches in v1: a delay is an automation with `for_s`, and
  a durable "wait" would need a scheduler of continuations.

### When a trigger fires

Device triggers are **edge-triggered**: they fire when an observation changes them from
not matching to matching. Each trigger's last observation lives in
`automations.trigger_states` and is updated under a row lock, ordered by the
observation's own time, so a late or duplicated event cannot fire anything. The first
observation only records the state (otherwise saving "above 26" on a hot day would fire
at once); saving an automation **primes** each trigger from the device's current value,
so the first real change is not lost.

With `for_s`, matching arms a timer (`fire_at`); it is cancelled if the trigger stops
matching first, and fired by the worker's timer sweep otherwise. Schedules keep their
next slot in `automations.schedules`, computed in the home's time zone: a time skipped
by a daylight-saving jump fires at the same offset as before the jump (an hour later on
the wall clock), a repeated time fires once, and a slot more than five minutes late
(the worker was down) is skipped rather than run hours late.

### Runs, exactly how often

Every firing becomes a row in `automations.runs`, recorded inside the transaction that
holds the automation's row lock: conditions, cooldown and the loop guard all see every
earlier run. `UNIQUE (automation_id, event_key)`, where the key is the stream entry,
the timer instant or the schedule slot, makes a redelivered event or two workers
sweeping the same timer run the automation **at most once**.

Commands are issued after that transaction commits, through `commands.public` (so they
go through the outbox and their ack lifecycle). If the worker dies in between, the run
stays `running` and is settled as `failed` after five minutes; its actions are not
retried. At most once is the safe side when the action is a lock.

| status       | meaning                                              |
|--------------|------------------------------------------------------|
| `running`    | recorded; commands being issued                      |
| `completed`  | every command was accepted for delivery              |
| `failed`     | some action could not be issued (device quarantined, scene deleted, interrupted) |
| `skipped`    | a condition did not hold (or could not be read)      |
| `suppressed` | cooldown, or the loop guard                          |

### Loop protection

Three layers, from cheapest to strictest:

1. **When saving**, the API builds the graph "A sets something B watches" over the
   home's automations (scenes included) and reports strongly connected components as
   `warnings`. It does not reject them: whether they loop depends on values.
2. **Causal depth.** Before a run issues its commands it marks each target device in
   Redis with its depth + 1 (60 s TTL). A state or telemetry event from a marked device
   carries that depth; a run at depth > 5 is suppressed.
3. **Rate.** More than 20 runs of one automation in a minute is suppressed.

Layers 2 and 3 **suspend** the automation (status `suspended`, with the reason),
drop its timers and schedules, and write `automation.suspended` to the audit log.
Enabling or editing it lifts the suspension: the person doing that is the one who was
asked to look. An integration test runs four automations that would toggle two lights
forever, with devices that obey, and checks the guard stops them.

### Editing

- `PUT` and `DELETE` take `If-Match: "<version>"` (the `ETag`); `PUT` without it is
  `428`, a stale one `412`. Every saved version goes to `automations.revisions`, which
  a trigger keeps append-only (including a final `deleted` revision).
- Saving resets the automation's trigger states and schedules for the new version.
  The engine re-checks the version under the row lock, so a worker holding a cached
  copy of the old version cannot act on it.
- Workers cache armed automations per home and check a per-home generation counter in
  Redis on each event (one `GET`); every change bumps it.

### Scenes

A scene is a named set of desired states (`automations.scenes`). Activating one issues a
command per device, as the person who activated it; automations reference scenes by id.
A scene used by an automation cannot be deleted (`409` names the automations).

### Authorization

| action                          | needs                                                    |
|---------------------------------|----------------------------------------------------------|
| read automations, runs, revisions | `view_home`, not a guest                               |
| create, edit, enable            | `manage_automations` **and** permission to send every command the automation (or its scenes) can send, as in ADR 0009: locks and cameras need `operate_locks` and a recent sign-in |
| activate a scene                | permission to send each of its commands (a guest only within their pass) |
| dry run                         | `manage_automations`                                     |

Automations run as `automation:<id>`, so every command they send is attributable, and
commands to locks and cameras are audited as before.

### Dry run

`POST /homes/{home}/automations/dry-run {definition, from, to}` (up to 7 days) replays
recorded telemetry and the schedule through the **same functions** the engine uses
(`TriggerState.observe`, `check_conditions`, `admit`). Device state and presence are not
kept as history (ADR 0008), so those triggers are reported as not replayable and those
conditions as unknown (assumed to hold, and listed per run).

### Consequences

- Good, because rules never slow ingestion, and worker replicas share events without
  running an automation twice (unique key, row locks; the cooldown race has a test that
  fails without the lock).
- Good, because firing semantics are explicit and tested, DST included, and the dry
  run answers with the engine's own rules.
- Good, because a loop stops itself within a few runs and tells someone why.
- Good, because one trace follows a sensor reading to the automation's command and the
  device's ack (verified live: ingestor, worker and simulator in one Tempo trace).
- Bad, because the event path is at most once across a crash right after a commit, and
  actions are at most once across a crash after a run is recorded.
- Bad, because an automation keeps running after its author leaves the home; it carries
  the authority of whoever last saved or enabled it. Revoking a member does not review
  their automations (yet).
- Bad, because the dry run cannot replay device state or presence.

### Confirmation

- Unit: the DSL (schema + semantic errors with paths), edge detection and holds,
  conditions, the guard, schedules across DST (with Hypothesis), the loop graph, the dry
  run, and the services against in-memory ports.
- Integration (`tests/integration/automations`): the stream (retry in order, dead
  letter, claiming from a dead worker, consumer group split, trace continuation); a
  telemetry message becoming a queued command in one trace; duplicate delivery; the
  cooldown race; timer and schedule sweeps by two workers; the four-automation loop;
  schema guards; and the HTTP API (If-Match, validation problems, roles, step-up for
  locks, scenes, dry run over Timescale).
- Metrics: `smarthome.automations.runs` (by status and cause),
  `smarthome.automations.suspended`, `smarthome.events.published`,
  `smarthome.events.consumed`, `smarthome.events.lag`.
