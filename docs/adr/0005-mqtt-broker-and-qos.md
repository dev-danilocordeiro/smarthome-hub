---
status: accepted
date: 2026-10-02
---

# 0005. Mosquitto with dynamic security; QoS chosen per message type

## Context and problem statement

Devices need an MQTT broker that:

- speaks MQTT 5 (user properties carry trace context, plus reason codes and session expiry);
- enforces TLS;
- gives every device its own credentials and an ACL limited to its own topics;
- can revoke or quarantine a device **at runtime**, disconnecting it immediately;
- runs in a laptop-sized compose stack.

We also need a deliberate QoS and retain choice for every message type.

## Considered options

1. **EMQX**: rich built-in auth/ACL (HTTP, database), rate limiting, Prometheus
   endpoint, dashboard.
2. **Mosquitto 2.1 + dynamic-security plugin**: per-client credentials and roles managed
   at runtime through `$CONTROL/dynamic-security/v1`.
3. **HiveMQ CE / VerneMQ**: other open-source brokers.

## Decision outcome

Chosen option: **2, Mosquitto 2.1 with the dynamic-security plugin.**

- **Licensing.** Mosquitto is EPL-2.0/EDL-1.0. EMQX moved to the Business Source License
  (5.9+); the current image (6.3) ships as "EMQX Enterprise" under BSL-1.1. For an
  open-source project, that was decisive.
- **Runtime identity management fits the domain.** The hub creates one dynsec client
  per device (username and client id = device id) and one role per device whose ACL
  lists exactly the device's topics. These come from `device_protocol.device_publishes`
  and `device_subscribes`, so the ACL and the protocol cannot drift apart.
  `deleteClient` revokes and `disableClient` quarantines. Both disconnect a live
  session immediately, which is verified by integration tests.
- **Small.** One process, about 10 MB of RAM, starts in under a second.

### Configuration

- A single TLS listener (8883). No plaintext listener at all, not even internal; the
  hub connects over TLS too.
- `allow_anonymous false`. Dynsec defaults deny publish and subscribe; roles grant.
- On first boot, the entrypoint runs `mosquitto_ctrl dynsec init` with the admin
  credentials from the environment. Only the hub uses that account, and it can manage
  identities but cannot read device traffic.
- Persistence is on, so retained presence/state and queued QoS 1 commands survive
  restarts.
- Bounded resources per client: 64 KiB packets, 20 in flight, 1000 queued.
- Dev certificates come from `scripts/gen-dev-certs.sh` (P-256 CA plus a server cert
  for `localhost`/`mqtt`). They are gitignored and regenerated per test run.

### QoS and retain

See the table in [docs/device-protocol.md](../device-protocol.md#delivery-guarantees).
Telemetry uses QoS 0; state, commands, acks and presence use QoS 1. State and presence
are retained; commands never are. QoS 2 is not used, because idempotency by
`message_id` and `command_id` is cheaper and is needed regardless.

### Last Will

Every device registers a retained `offline`/`connection_lost` presence as its Will.
The simulator emulates power loss with MQTT 5 DISCONNECT reason code `0x04`
("disconnect with Will message"), which makes the broker publish the Will exactly as
on a dropped connection. The alternative, closing the socket under the client
library, left the library waiting on a dead socket for its full timeout.

### Consequences

- Good: credentials, ACLs, revocation and quarantine are runtime operations with
  integration tests against the real broker:
  - TLS trust and wrong password;
  - client-id pinning;
  - cross-device publish and subscribe denied;
  - Will on drop;
  - revocation and quarantine kick the device.

  A mutation test (granting `v1/#`) makes the isolation test fail.
- Good: shared subscriptions (`$share/…`) let ingestors scale out in phase 6.
- Bad: Mosquitto has no per-client **rate limiting**. Rate limiting and quarantine
  decisions move to the ingestor (phase 6), which counts messages per device and calls
  `disableClient` past a threshold. This is arguably the better place anyway, because
  the policy needs domain context (device type, expected cadence).
- Bad: no native Prometheus endpoint. Broker metrics come from `$SYS` topics, which
  the hub turns into OTel metrics (phase 10).
- Bad: dynsec state lives in a JSON file in the broker volume. The hub's device
  registry (phase 5) is the source of truth; a reconciliation job can rebuild dynsec
  from it.
