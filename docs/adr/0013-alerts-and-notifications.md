---
status: accepted
date: 2026-10-03
---

# 0013. Alerts and notifications: one live alert per key, an inbox, a delivery queue

## Context and problem statement

Some things in a home need a person: a lock that dropped off the network, a sensor
whose battery is about to die, a month that is going over its energy budget. The hub
already sees all of these (device events, ADR 0010; energy, ADR 0012). It now has to
decide **when** something is worth telling, **whom** to tell, **how** (in the app,
email, a webhook into the household's own tools), and how to do that without spamming
anyone when a sensor flaps or two workers see the same event.

## Decision drivers

- A flapping condition produces one notification, not one per event.
- Correct with several worker replicas and an at-least-once event stream.
- Out-of-order events must not undo newer ones.
- People choose what reaches their mailbox, and when; critical things still get through.
- Webhooks must be verifiable by the receiver and must not become a way to reach the
  hub's own network (SSRF).
- A delivery failure never loses the notification and never blocks the next one.

## Considered options

1. Notify straight from the event handler (send the email inside the consumer).
2. Automations (ADR 0010) with a "notify" action.
3. **An alert lifecycle with a unique live alert per key, an inbox, and a delivery
   queue drained by the worker.**

## Decision outcome

### Alerts

An alert has a **key** that names the condition, not the occurrence:
`device_offline:<device>`, `low_battery:<device>`, `energy_budget:<yyyy-mm>:<percent>`.
Its life is `pending → open → resolved`:

| Kind | Raised | Cleared | Severity |
|---|---|---|---|
| `device_offline` | presence offline, then **5 min grace** (`pending`) | presence online | critical for locks and cameras, else warning |
| `low_battery` | battery < 15 % | battery ≥ 20 % (hysteresis) | same |
| `energy_budget` | month-to-date ≥ 80 % / 100 % of the budget | the month ends | info / warning |

- **One live alert per key** is a partial unique index,
  `(home_id, key) WHERE status IN ('pending','open')`. Raising is
  `INSERT … ON CONFLICT DO NOTHING`; only the worker whose insert succeeded fans out.
  Resolving is an `UPDATE … WHERE status IN (…)`; only one worker sees the row change.
  Eight concurrent handlers of the same event open one alert and one set of inbox
  entries (integration test).
- **Ordering.** Every alert row keeps the observation time of its last transition;
  an event older than the newest observation for the key is ignored, so a late
  "battery 80 %" cannot resolve an alert raised by a newer "battery 10 %".
- **Grace period.** A pending offline alert opens when the worker's timer finds it due
  **and** the devices module still says the device is offline. If the device came back
  and the `online` event was lost (the stream is best effort), the alert is dropped,
  not opened. Dropped pending alerts are kept as resolved-without-opening (they guard
  ordering) and never shown.
- People are told when an alert **opens** and when it **resolves**, never while it
  stays open. Members can acknowledge an open alert ("I'm on it"); it stays open until
  the condition clears.

### Notifications

Opening or resolving an alert writes, **in the same transaction**:

- an **inbox** entry for every active member except guests (a guest pass is for using
  some devices, not for being told about the house; scoped guests do see alerts of their
  own devices in the alerts list);
- **email** deliveries for new alerts, per member preferences: on/off, minimum severity
  (default warning), and quiet hours in home time (wrapping past midnight). Quiet hours
  delay a delivery to their end; **critical alerts ignore them**. A delayed email whose
  alert resolved meanwhile is skipped. At most 10 emails per address per hour;
- a **webhook** delivery for both transitions, if the home has one.

Live clients get an `alert` message over the home's WebSocket (ADR 0011): the alert
engine publishes `DeviceEventKind.ALERT` to the device event stream after commit
(`device_id` empty for home-wide alerts, so scoped guests never see them). The web app's
bell refetches the inbox when one arrives.

The inbox only shows entries from homes the person is a member of **now**.

### Delivery

`notifications.deliveries` is a queue: claimed with `FOR UPDATE SKIP LOCKED`, attempted,
settled. Transient failures (SMTP errors, timeouts, 5xx, 408/409/425/429) retry with
exponential backoff (30 s doubling, capped at 1 h, 8 attempts); permanent ones (other
4xx, refused recipients) fail at once. Delivery is **at least once**: a worker that dies
between sending and committing leaves the row queued. Webhook receivers deduplicate on
the `Idempotency-Key` header (stable per alert transition); a duplicate email is the
accepted cost.

Email goes over plain SMTP (stdlib `smtplib` on a thread). In development, **Mailpit**
catches everything (`http://localhost:8025`); nothing leaves the machine.

### Webhooks

One per home, managed by owners. Payloads are JSON (`alert.opened`, `alert.resolved`,
`ping`), signed like Stripe's:
`X-Smarthome-Signature: t=<unix>,v1=hex(HMAC-SHA256(secret, "<t>.<body>"))`. Receivers
recompute it and reject timestamps older than five minutes (replay). The secret is
generated by the hub, shown **once** (on creation or rotation) and used for queued
deliveries too after a rotation.

SSRF: outside development, URLs must be `https`, carry no credentials, and resolve only
to public addresses (no loopback, RFC 1918, link-local such as the cloud metadata
endpoint, unique-local IPv6, unspecified). The check runs when the URL is saved **and**
before every send; redirects are not followed. DNS can change between the check and the
connection (rebinding); pinning the resolved address under TLS is left for later (risk R3 in the
[threat model](../security/threat-model.md)).

### Consequences

- Good, because flapping, duplicates and races cost nothing: the schema decides.
- Good, because the alert, its inbox entries and its deliveries commit together; a crash
  can delay a notification but not lose or half-record it.
- Good, because email and webhooks are isolated from event handling: a slow SMTP server
  slows the queue, not the stream.
- Bad, because a notification can arrive twice (at least once). Webhooks can dedupe;
  email cannot.
- Bad, because alert kinds are code, not configuration. Anything custom is an
  automation's job.

### Confirmation

- Unit tests: rules (hysteresis, grace, budget thresholds), quiet hours and severity,
  webhook signatures and URL validation, the SSRF guard on literal addresses, the
  dispatcher's retry/skip/give-up policy.
- Integration tests (Postgres, Redis, Mailpit, a local HTTP receiver): fan-out, eight
  racing handlers open one alert, stale events, grace period with a lost `online`,
  budgets per month, the consumer group path, email delivered to Mailpit, webhook
  signed and retried on 5xx, not retried on 4xx, two dispatchers never double-send.
- Metrics: `smarthome.alerts.transitions`, `smarthome.notifications.deliveries`
  (by channel and outcome), `smarthome.notifications.delivery.delay`.

## Pros and cons of the options

### Notify from the event handler

- Good, because it is the least code.
- Bad, because a slow SMTP server stalls the event stream, retries re-run the handler
  (duplicate emails), and nothing remembers that an alert is already open.

### A "notify" action in automations

- Good, because users could build their own alerts.
- Bad, because every household would have to rebuild "tell me when the lock goes
  offline", and edge-triggered rules have no notion of an alert that stays open and
  resolves. Both can coexist later: a notify action could enqueue an inbox entry.
