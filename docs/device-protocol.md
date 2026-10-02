# Device protocol v1

How a device (simulated or real) talks to the hub. The machine-readable contract lives in
[`packages/device-protocol`](../packages/device-protocol): JSON Schemas in
`src/device_protocol/schemas/v1/` and the topic/QoS table in `topics.py`. This page
explains it; if the two ever disagree, the package wins and this page is a bug.

## Transport

- **MQTT 5** over **TLS 1.2+** on port `8883`. There is no plaintext listener.
- Each device has its **own credentials**. The username and the MQTT client id are both
  the device id; the broker refuses a client id that does not belong to the credentials.
- The broker's **ACL** lets a device publish only to its own `telemetry`, `state`,
  `commands/ack` and `presence` topics, and subscribe only to its own `commands` topic.
  Anything else is denied (publishes are dropped, subscriptions get SUBACK `0x87`).
- **Revocation** deletes the credentials and disconnects a live session at once.
  **Quarantine** disables the client the same way, but can be reversed.
- Keep-alive is 30 s for the simulator (max 300 s at the broker). The maximum packet is
  64 KiB, and the hub additionally rejects payloads over 8 KiB.

## Topics

```
v1/homes/{home_id}/devices/{device_id}/telemetry      device → hub
v1/homes/{home_id}/devices/{device_id}/state          device → hub   (retained)
v1/homes/{home_id}/devices/{device_id}/commands       hub → device
v1/homes/{home_id}/devices/{device_id}/commands/ack   device → hub
v1/homes/{home_id}/devices/{device_id}/presence       device → hub   (retained, Last Will)
```

Ids match `^[A-Za-z0-9_-]{1,64}$`, so they can never inject `/`, `+` or `#`.
The leading `v1` versions the **topic layout**. A future `v2` can run in parallel,
with its own ACLs, while devices migrate.

The hub subscribes with wildcards (`v1/homes/+/devices/+/telemetry`). With several
ingestor replicas it uses MQTT 5 shared subscriptions
(`$share/ingestor/v1/homes/+/devices/+/telemetry`), so each message is processed once.

## Delivery guarantees

| Kind          | QoS | Retained | Why |
|---------------|:---:|:--------:|-----|
| telemetry     |  0  |    no    | High volume, and the next sample supersedes a lost one. Acking every reading would cost more than the reading is worth. |
| state         |  1  |   yes    | The device's actual state must not be lost, and a new subscriber should get it immediately. |
| commands      |  1  |    no    | Must arrive. Duplicates are expected and neutralised by `command_id`. Not retained: a stale command must never fire on reconnect. |
| commands/ack  |  1  |    no    | The hub's command state machine depends on it. |
| presence      |  1  |   yes    | "Is it online?" must have an answer without waiting for the next message. |

QoS 2 is not used anywhere. Its exactly-once handshake costs two extra round trips,
and it still does not make *processing* idempotent. Idempotency by message and
command id is cheaper, and it is needed anyway.

## Payloads

Every payload is a JSON object with `"schema_version": "1"` and no unknown fields
(`additionalProperties: false`). Timestamps are RFC 3339 from the device clock.

### telemetry

```json
{"schema_version": "1", "message_id": "q0tzM2V3a1Fv", "seq": 42,
 "ts": "2026-10-02T12:00:00Z",
 "readings": {"temperature_c": 21.5, "humidity_pct": 48, "battery_pct": 93}}
```

- `message_id` (8–64 url-safe chars) lets the hub drop duplicates.
- `seq` grows monotonically per boot; a reset to 0 means the device rebooted.
- `readings` holds known metrics only, each with physical bounds (for example humidity
  0–100 %). Out-of-range values are rejected, not clamped.

### state

```json
{"schema_version": "1", "message_id": "a1b2c3d4e5", "ts": "2026-10-02T12:00:00Z",
 "reported": {"on": true, "brightness_pct": 70}}
```

This is the `reported` side of the digital twin. A device sends it on connect and
whenever its state changes, whether by command or locally.

### commands

```json
{"schema_version": "1", "command_id": "3f0c…", "issued_at": "…", "expires_at": "…",
 "action": "set_state", "desired": {"locked": false}}
```

- `action` is `set_state` (requires `desired`), `identify` or `reboot` (neither of which
  may carry `desired`).
- A device **must**:
  1. execute a `command_id` at most once, re-sending its original final ack if the
     command is redelivered;
  2. answer `expired` instead of executing when it receives the command after
     `expires_at`;
  3. answer `rejected` when `desired` names properties it does not have.

### commands/ack

```json
{"schema_version": "1", "command_id": "3f0c…", "ts": "…", "status": "applied"}
```

`received` is sent first. The final status is one of `applied`, `rejected`, `failed`
or `expired`, plus an optional `reason`.

### presence

```json
{"schema_version": "1", "status": "online", "reason": "boot", "ts": "…", "firmware": "1.4.2"}
```

At connect, the device registers this as its **Last Will** (QoS 1, retained):

```json
{"schema_version": "1", "status": "offline", "reason": "connection_lost"}
```

The broker publishes the Will when the connection dies without a clean DISCONNECT
(power loss, network loss, keep-alive timeout). Devices announce `online` with reason
`boot` or `reconnect`, and publish `offline` with reason `shutdown` before a clean exit.

## Sessions

Devices connect with `clean_start = false` and a session expiry of one hour, so QoS 1
commands sent while a device is briefly offline are queued and delivered on
reconnect. Expired ones are then answered with `expired`.

## Trace context

From phase 7, commands carry W3C `traceparent` / `tracestate` as MQTT 5 user
properties, and devices copy them onto their acks. That lets one trace cover
click → API → broker → device → ack. Payloads stay free of tracing fields.
