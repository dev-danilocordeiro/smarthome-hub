---
status: accepted
date: 2026-10-02
---

# 0004. Homes as tenants, per-home roles, and a tamper-evident audit log

## Context and problem statement

One person can live in one home, own a holiday house and be a guest at a friend's
place for a weekend. Authorization therefore cannot be global ("is an admin") but per
home, and sometimes per device (a guest who may open only the front door until Sunday).
Sensitive actions (locks, invitations, revocations) must be answerable later, in a way
that survives a curious database administrator.

## Decision outcome

### Tenancy and roles

- A **home** is the tenant. Users exist only in Keycloak; the hub stores their `sub`
  plus a profile cache (`identity.users`) for display names.
- Access is a **membership** (home, user, role) with history. Revoked rows stay, and
  a partial unique index allows one live membership per user and home.
- Roles and permissions:

  | Permission          | owner | resident | guest        | viewer |
  |---------------------|:-----:|:--------:|:------------:|:------:|
  | view home           |   ✓   |    ✓     |      ✓       |   ✓    |
  | control devices     |   ✓   |    ✓     | ✓ (in scope) |        |
  | operate locks       |   ✓   |    ✓     | ✓ (in scope) |        |
  | manage devices      |   ✓   |    ✓     |              |        |
  | manage automations  |   ✓   |    ✓     |              |        |
  | manage members      |   ✓   |          |              |        |
  | view audit log      |   ✓   |          |              |        |

- **Guests always expire** (at most 30 days) and may be limited to a device scope.
  The rules are enforced in the domain constructors **and** as `CHECK` constraints:
  guests expire, only guests have a scope, owners never expire.
- **Invitations** are single-use links. Only `sha256(token)` is stored. Redeeming
  locks the invitation row, so two people racing on one link cannot both get in. Owner
  is never granted by invitation.
- **A home always keeps an owner.** Removing an owner locks all live memberships of
  the home in id order and re-counts owners. Without the lock, two owners removing
  each other at the same moment both succeed; the integration test demonstrates this
  by failing when the lock is removed.
- **Non-members get 404, not 403**, for every `/homes/{id}/…` route, so home ids cannot
  be probed. 403 is returned only to members who lack a permission.
- Other modules authorize through `identity.public.require_home_access(permission)`.
  Device-scoped checks pass the device id to `IdentityService.access`.

### Audit log

- `audit.entries` is a shared, append-only table. It is written on the caller's
  connection, so an entry exists if and only if the audited change committed.
- **Append-only in the database**: triggers reject `UPDATE`, `DELETE` and `TRUNCATE`.
- **Tamper-evident**: each entry stores `sha256(prev_hash ‖ canonical entry)`, chained
  per tenant. Inputs are normalized the way Postgres stores them (UTC timestamps,
  JSON-native details), so verification on read is exact.
  - Writers to one tenant's chain serialize on a transaction-scoped advisory lock.
  - A unique index on `(tenant, prev_hash)` guarantees one successor per link even if
    that lock were bypassed.
- `GET /homes/{id}/audit` returns entries together with `chain_intact`. A superuser
  can disable the trigger and rewrite a row, but not without breaking the chain, which
  the integration test demonstrates.
- Every entry carries the `trace_id` of the request that caused it, linking the audit
  trail to Tempo.

### Consequences

- Good: authorization questions have one answer, `Membership.allows`, property-tested
  over the whole role × permission space.
- Good: the audit log's guarantees are checked by the database and by tests, not just
  promised in code.
- Bad: chain verification is linear in a tenant's entries. Fine at this scale; a
  periodic checkpoint (signed hash of entry N) would bound it later.
- Bad: ownership transfer and role changes are not implemented yet. Today the
  workaround is revoke plus re-invite, and owners cannot be added except at creation.
  Planned with the frontend phase.
