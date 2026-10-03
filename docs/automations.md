# Automations

An automation says: **when** any of its triggers fires, **if** all of its conditions
hold, **do** its actions in order. The definition is a JSON document (DSL v1), validated
against the JSON Schema served at `GET /automations/schema`. Why it works the way it
does: [ADR 0010](adr/0010-automations-event-stream-and-loop-protection.md).

```json
{
  "schema_version": "1",
  "triggers": [
    { "type": "telemetry", "device_id": "motion-1a2b3c", "metric": "motion", "op": "eq", "value": true }
  ],
  "conditions": [
    { "type": "time", "after": "18:00", "before": "06:00" },
    { "type": "telemetry", "device_id": "climate-4d5e6f", "metric": "illuminance_lux", "op": "lt", "value": 40 }
  ],
  "actions": [
    { "type": "command", "device_id": "light-7a8b9c", "action": "set_state", "desired": { "on": true, "brightness_pct": 70 } }
  ],
  "cooldown_s": 60
}
```

## Triggers

| type           | fields                                             | fires when                                   |
|----------------|----------------------------------------------------|----------------------------------------------|
| `telemetry`    | `device_id`, `metric`, `op`, `value`, `for_s`      | a reading starts matching                    |
| `device_state` | `device_id`, `property`, `op`, `value`, `for_s`    | a reported twin property starts matching     |
| `presence`     | `device_id`, `status` (`online`/`offline`), `for_s`| the device goes online / offline             |
| `schedule`     | `at` (`HH:MM`), `weekdays` (`mon`..`sun`)          | that wall-clock time in the home's time zone |

`op` is one of `eq`, `ne`, `gt`, `gte`, `lt`, `lte`; ordering operators take numbers.
Metrics and properties are those of [device protocol v1](device-protocol.md); boolean
metrics (`motion`, `contact_open`) support `eq` and `ne`.

Device triggers are **edge-triggered**: "temperature above 26" fires when a reading
crosses 26, not on every reading above it. It fires again only after a reading at or
below 26. With `for_s`, it fires once the trigger has kept matching that long, and not
at all if it stops matching first: "no motion for 5 minutes" is

```json
{ "type": "telemetry", "device_id": "motion-1a2b3c", "metric": "motion", "op": "eq", "value": false, "for_s": 300 }
```

Schedules follow the home's time zone, including daylight saving time: a time that is
skipped that day fires an hour later on the wall clock, a time that happens twice fires
once. A slot missed by more than five minutes (the worker was down) is skipped.

## Conditions

All must hold when a trigger fires. A condition the hub cannot read (a device that never
reported) counts as not holding.

| type           | fields                                                  |
|----------------|---------------------------------------------------------|
| `time`         | `after`, `before` (`HH:MM`, either optional; wraps midnight when `after > before`), `weekdays` |
| `device_state` | `device_id`, `property`, `op`, `value`                  |
| `presence`     | `device_id`, `status`                                   |
| `telemetry`    | `device_id`, `metric`, `op`, `value`, `max_age_s` (default 900): the latest reading, if recent enough |

## Actions

| type      | fields                                                        |
|-----------|---------------------------------------------------------------|
| `command` | `device_id`, `action` (`set_state` with `desired`, or `identify`) |
| `scene`   | `scene_id`                                                    |

Commands go through the normal command pipeline (outbox, acks, timeouts) and are issued
by `automation:<id>`. One refused command (a quarantined device) does not stop the rest.

## Runs and loop protection

`GET /homes/{home}/automations/{id}/runs` lists what happened and why:
`completed`, `failed`, `skipped` (with the condition that failed) or `suppressed`
(cooldown, or the loop guard). Each run has a `trace_id`.

An automation is **suspended** if it is triggered by a chain of more than five
automation runs (A's command changes a device that triggers B, whose command...) or runs
more than 20 times in a minute. Saving also returns `warnings` when automations could
trigger each other. Enable it again (`POST .../enable`) or edit it to resume.

## Editing

```http
POST   /homes/{home}/automations                {"name", "description", "enabled", "definition"}
GET    /homes/{home}/automations/{id}           ETag: "3"
PUT    /homes/{home}/automations/{id}           If-Match: "3"   (428 without, 412 if stale)
DELETE /homes/{home}/automations/{id}
POST   /homes/{home}/automations/{id}/enable | /disable
GET    /homes/{home}/automations/{id}/revisions every saved version, kept forever
```

Invalid definitions return `422` with `type: urn:smarthome:problem:invalid-automation`
and a `path` into the document (`triggers/0/value`).

An automation that can send a command to a lock or camera (directly or through a scene)
needs the same permissions to save as sending that command yourself: `operate_locks`
and a sign-in from the last five minutes.

## Scenes

```http
POST /homes/{home}/scenes               {"name": "Movie", "states": [{"device_id": "...", "desired": {"on": false}}]}
POST /homes/{home}/scenes/{id}/activate 202 {"outcomes": [{"device_id", "command_id", "error"}]}
```

## Dry run

```http
POST /homes/{home}/automations/dry-run  {"definition": {...}, "from": "2026-10-01T00:00:00Z", "to": "..."}
```

Replays up to seven days of recorded telemetry and the schedule through the engine's own
rules and lists when the automation would have run, been skipped or suppressed. Device
state and presence are not kept as history: those triggers cannot be replayed (reported
in `warnings`) and those conditions are listed as `unknown_conditions`.
