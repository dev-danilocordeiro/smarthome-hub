---
status: accepted
date: 2026-10-03
---

# 0014. HTTP API conventions

## Context and problem statement

Seven modules expose HTTP routes, written over nine phases. Without shared rules each
would answer errors, conflicts, long-running work and foreign resources differently, and
the web client (and anyone integrating) would have to learn every module separately.
These conventions grew phase by phase; this record states them in one place so new
routes follow them.

## Decision drivers

- A client must be able to act on an error without parsing English.
- Nothing about other tenants may leak through status codes.
- Concurrent edits by two members must not silently overwrite each other.
- The contract is generated from the code and checked, not written by hand.

## Decision outcome

**Resources are scoped by path.** Everything that belongs to a home lives under
`/homes/{home_id}/…`; what belongs to the signed-in person lives under `/me/…`; device
provisioning, which has no session, under `/provisioning/…`. Authorization is declared
per route (`require_home_access(Permission.X)`), so a route without a permission is
visible in review.

**Errors are RFC 9457 problem details** (`application/problem+json`, built by
`shared/http/problems.py`). `type` is `about:blank` unless the client must act on the
specific problem; then it is a URN and carries what the client needs:

| `type` | Status | Extra members | Client action |
|---|---|---|---|
| `urn:smarthome:problem:reauthentication-required` | 401 | `login_url` | send the person to sign in again, then retry |
| `urn:smarthome:problem:invalid-automation` | 422 | `path` (e.g. `triggers/0/value`) | point at the field in the editor |

**Foreign resources are `404`.** A home you are not a member of, a device outside a
guest's pass, an alert of another home: all answer exactly like a resource that does not
exist, so ids cannot be probed ([ADR 0004](0004-tenancy-roles-and-audit-log.md)). `403`
is reserved for members who may see the resource but not do this to it.

**Edits are versioned.** Mutable configuration (automations, scenes, tariffs) carries an
integer version, returned as `ETag: "<n>"`. Updates send `If-Match`; without it the API
answers `428 Precondition Required`, with a stale one `412 Precondition Failed`. The
first creation of a singleton (a home's tariff) needs no `If-Match`; every later change
does. Shared helpers: `shared/http/preconditions.py`.

**Work that waits on a device is asynchronous.** `POST …/commands` answers `202` with the
command's id and `pending` status; the outcome arrives over the live socket or from
`GET …/commands/{id}` ([ADR 0009](0009-commands-outbox-and-trace-propagation.md)).

**Values that must stay exact travel as strings.** Money and prices are decimal strings
(`"0.891234"`), never JSON numbers ([ADR 0012](0012-energy-accounting-from-counters.md)).
Times are ISO 8601 with an offset; the server stores UTC and reports home-local times
where the question is local (energy buckets, quiet hours).

**Lists that grow with time are bounded.** Automation runs, recent commands, alerts, the audit log and the inbox take a
`limit` with a maximum; the inbox pages by `before` (the `created_at` of the last item
seen), never by offset. Lists bounded by the home itself (devices, members, scenes) are
returned whole.

**Unsafe methods need the CSRF header** and an allowed `Origin`
([ADR 0003](0003-bff-sessions-and-csrf.md)). Responses carry `Cache-Control: no-store`.

**The contract is generated.** FastAPI produces the OpenAPI document; `make gen-client`
exports it to `packages/contracts` and generates the web app's TypeScript types; CI fails
if either is stale. There is no URL versioning (`/v1`): the only client is the BFF's own
web app, released together. The device protocol, which real firmware depends on, is
versioned separately (`v1/…` topics, `schema_version` in every message,
[ADR 0005](0005-mqtt-broker-and-qos.md)).

### Consequences

- Good, because errors, conflicts and foreign resources behave the same in every module,
  and the web client handles them in one place (`api/client.ts`).
- Good, because a renamed field breaks the build, not production.
- Bad, because a third-party client would need URL versioning or a compatibility policy
  first; adding `/v1` is a routing change when that day comes.

### Confirmation

- Integration tests per module assert 404 for non-members, 428/412 for edits, problem
  `type`s for re-authentication and invalid automations.
- CI: OpenAPI and TypeScript types up to date (`make contracts-check`).
