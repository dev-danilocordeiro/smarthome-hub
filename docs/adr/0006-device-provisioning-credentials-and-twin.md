---
status: accepted
date: 2026-10-02
---

# 0006. Device provisioning, credentials, quarantine and the digital twin

## Context and problem statement

A resident unboxes a smart plug. Some questions follow:

- How does the plug become *this home's* plug, with credentials nobody else has?
- How is a misbehaving device cut off right away, and a lost one revoked for good?
- How does the hub know, at any moment, whether the device is online and what state
  it is really in, as opposed to the state someone asked for?

## Decision outcome

### Pairing with a short code

1. A member with `manage_devices` asks for a **pairing code**: 8 Crockford base32
   characters shown as `K7Q4-M2XD` (40 bits). The code can be restricted to one device
   kind and preset with a name and room.
   - Only `sha256(code)` is stored.
   - It is valid for 10 minutes and works once.
   - Input is normalised, so `k7q4 m2xd`, `K7Q4-M2XD` and look-alikes (`O`→`0`,
     `I`/`L`→`1`) are equivalent.
2. The device calls the **unauthenticated** `POST /provisioning/claim {code, kind,
   firmware}`. In exchange it receives:
   - a hub-generated device id;
   - a random 256-bit broker password (returned exactly once; the hub keeps it only
     inside the broker);
   - the broker endpoint and its topic names.
3. Guessing is hopeless rather than merely hard. Claims are rate limited per client
   IP, and failures are counted separately. 40 bits, 10 minutes, a handful of tries per
   minute: the odds of hitting any live code are negligible.

The claim spans two systems (broker identity store and Postgres), so it is a
**saga with compensation**:

1. Reject early if the code is unknown, expired or of the wrong kind. Nothing is
   created at the broker.
2. Create the broker client and its per-device ACL role.
3. In one transaction: lock the code row, re-check it, insert device and twin, mark
   the code claimed, append the audit entry.
4. If step 3 fails (a concurrent claim won the code, or the database is down), delete
   the broker client again.

A failed claim therefore never leaves a usable login behind. An integration test races
two claims on one code: exactly one succeeds, and the loser's broker credentials are
refused.

### Lifecycle: broker first, database second

`quarantine` (reversible) disables the broker client, `release` re-enables it, and
`revoke` deletes it. Each call reaches the **broker first**; the status change and
audit entry are recorded only after the broker confirmed. If the broker is
unreachable, the call fails (503) and nothing changes. If the database write fails
afterwards, the device has already lost access. Both outcomes err on the safe side
for security. Mosquitto disconnects a live session the moment its client is disabled
or deleted (integration-tested).

### Digital twin

`devices.twins` stores `desired` and `reported` (jsonb) with their timestamps.

- `reported` comes only from the device's retained `state` messages. It is ordered by
  the device's timestamp, so a duplicate or out-of-order message is ignored, and it is
  checked against the properties the device kind exposes (`device_protocol.STATE_PROPERTIES`).
- `desired` is written by commands (phase 7). `delta()` lists the desired values not yet
  reported; `in_sync` is `delta() == {}`. Alerting on a long-lived delta comes with
  notifications (phase 10).

### Presence and the ingestor

The ingestor is now a real process (same image as the API, entrypoint
`smarthome-ingestor`). It consumes `presence` (including Last Wills) and `state`, and
hands them to `devices.public`, which updates Postgres and a Redis read model
(`device:{id}`). Dashboards read Redis.

Four details came out of running it against a real broker. Each is now enforced by
code or tests:

1. **Presence is ordered by the hub's clock.** A Will is composed at connect time and
   has no timestamp. Ordering a device-stamped `online` against a hub-stamped
   `offline` would mix clocks, and a device whose clock runs ahead would never be
   seen offline. An integration test uses a device clock in 2099.
2. **Retained messages are never delivered to shared subscriptions** (MQTT 5 rule).
   Presence and state are last-value topics; a restarted ingestor catches up by
   receiving the retained copies. So they use **plain** subscriptions on every replica,
   whose handlers are idempotent and drop stale messages. High-volume telemetry and
   acks use `$share/ingestor/...` so replicas split them.
3. **No persistent session for the ingestor.** With shared subscriptions, the
   persistent session of a replica that died keeps receiving its share until it
   expires, and those messages are stranded. Clean sessions lose nothing that matters:
   last-value topics are retained, telemetry is QoS 0, and handlers are idempotent.
4. **The hub's MQTT account is reconciled on every start** (ACLs and password).
   Mosquitto checks `$share/<group>/...` subscriptions against an ACL naming that exact
   filter. A role deleted under a client leaves a dangling link that makes
   `addClientRole` fail until the link is removed; the reconciliation handles that.
   The consumer also checks SUBACK codes and refuses to run with a denied
   subscription, rather than looking like a quiet broker.

### Consequences

- Good: credentials are per device, single-purpose, and revocable in under a second.
  No shared secrets ship in firmware.
- Good: the twin makes "what we asked for" and "what the device did" explicit, which
  phase 7's command flow and the frontend build on.
- Bad: the pairing code travels from the resident's screen to the device out of band
  (typed, QR, BLE). That channel is out of scope here; a production device would also
  carry a factory attestation.
- Bad: a claim leaves a short window in which the device exists in the broker before it
  exists in Postgres. During it the device can connect, but the ingestor ignores
  unknown devices, so nothing it sends has an effect.
