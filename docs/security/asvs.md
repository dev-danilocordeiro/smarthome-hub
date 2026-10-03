# OWASP ASVS self-assessment

A chapter-by-chapter self-assessment against **OWASP ASVS 5.0, Level 2**, the level for
applications that handle sensitive data (here: physical access and occupancy). It is a
map from each chapter to what the code does and where to check it, not a certification;
requirement numbers are deliberately not quoted, so this page does not drift from the
standard's own numbering. Threats behind each control: [threat model](threat-model.md).

Status: **Met** (the chapter's L2 intent is covered and tested), **Partial** (covered
with a known gap, listed), **N/A** (the feature does not exist here).

| Chapter | Status | Summary |
|---|---|---|
| V1 Encoding and Sanitization | Met | No HTML rendering on the server, parameterised SQL everywhere |
| V2 Validation and Business Logic | Met | Schemas at every edge, invariants in the domain and the database |
| V3 Web Frontend Security | Met | Strict CSP, cookie flags, no tokens in the browser |
| V4 API and Web Service | Met | Problem details, CSRF on unsafe methods, WebSocket origin checks |
| V5 File Handling | N/A | No uploads or file downloads |
| V6 Authentication | Met | Delegated to Keycloak; brute-force protection, passkeys, step-up |
| V7 Session Management | Met | Opaque server-side sessions, absolute and idle limits, revocation on open sockets |
| V8 Authorization | Met | Per-home roles, device scopes, deny-by-default, tested for every role |
| V9 Self-contained Tokens | Met | ID tokens fully validated; no JWTs issued by the hub |
| V10 OAuth and OIDC | Met | Confidential client, authorization code + PKCE S256, refresh rotation |
| V11 Cryptography | Partial | Standard primitives only; two secrets stored in clear (R2) |
| V12 Secure Communication | Partial | TLS for MQTT; HTTP edge without TLS in development (R4) |
| V13 Configuration | Partial | Hardened containers, scanned dependencies; app uses the DB owner role (R1) |
| V14 Data Protection | Met | No-store caching, minimal data to webhooks, retention policies |
| V15 Secure Coding and Architecture | Met | Module boundaries enforced, concurrency tested, dependency inventory |
| V16 Security Logging and Error Handling | Met | Structured logs with trace ids, hash-chained audit log, uniform errors |
| V17 WebRTC | N/A | No WebRTC |

## V1 Encoding and Sanitization: Met

- Every SQL statement uses bound parameters (`sqlalchemy.text` with `:params`); the few
  f-strings interpolate module constants only (column lists, literal tuples), each marked
  and explained (`# noqa: S608`). Ruff's bandit rules run in CI.
- The API returns JSON only; the SPA renders through React (no `dangerouslySetInnerHTML`).
- The automation DSL is data validated against a JSON Schema, never evaluated as code.

## V2 Validation and Business Logic: Met

- HTTP bodies: Pydantic models with lengths, patterns, ranges (e.g. prices as decimal
  strings, `^\d{1,4}(\.\d{1,6})?$`). Device messages: JSON Schema per message type
  ([device protocol](../device-protocol.md)).
- Domain invariants in constructors (tariff periods, quiet hours, memberships) and again
  as `CHECK` constraints and unique indexes in Postgres.
- Business-logic abuse: single-use pairing codes and invitations under race (tests),
  one live alert per condition (unique index, race test), loop protection for
  automations, step-up before operating locks, flood quarantine for devices.

## V3 Web Frontend Security: Met

- API responses: `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`,
  `nosniff`, `Referrer-Policy: no-referrer`, COOP/CORP, HSTS, `Cache-Control: no-store`
  (`shared/http/security_headers.py`).
- Cookies: `__Host-` prefix, `Secure`, `HttpOnly`, `SameSite=Strict` for the session.
- The browser never holds an access, refresh or ID token (BFF, ADR 0003).
- The PWA service worker caches the app shell only, never `/api` (ADR 0011).

## V4 API and Web Service: Met

- Unsafe methods require a CSRF header and an allowed `Origin`; WebSocket handshakes
  require an allowed `Origin` (cross-site WebSocket hijacking).
- Errors are RFC 9457 problem details without stack traces; non-members get `404`.
- Optimistic concurrency with `ETag`/`If-Match` on mutable configuration (ADR 0014).
- The OpenAPI document is generated from the code and checked in CI; the web client is
  typed from it.

## V6 Authentication: Met

- Keycloak: brute-force detection, password policy, passkeys; the hub never sees
  passwords.
- Re-authentication for sensitive operations: locks and cameras require an
  authentication from the last five minutes (`prompt=login`, `max_age=0`).
- No default credentials outside the documented development realm (R8).

## V7 Session Management: Met

- 256-bit random session ids, stored hashed in Redis; absolute lifetime (12 h) and
  Keycloak idle timeout; logout ends both the BFF session and the IdP session.
- Long-lived WebSockets re-check session and membership every 60 s and close with 4401
  or 4403.
- Parallel requests refresh tokens once (lock), and refresh tokens rotate.

## V8 Authorization: Met

- Deny by default: every route declares the permission it needs
  (`require_home_access(Permission…)`); a role matrix lives in one place.
- Object level: home membership on every `/homes/{id}` route, device scope for guests,
  home-wide data refused to scoped guests, inbox filtered to current memberships.
- An automation's author must be allowed every command it contains.
- Tested per role in integration tests and by the E2E "not a member" scenario.

## V9 Self-contained Tokens: Met

- ID tokens: signature (JWKS with caching and rotation), `iss`, `aud`, `exp`, `nonce`.
- The hub issues no JWTs; its own credentials are opaque (sessions, pairing codes).

## V10 OAuth and OIDC: Met

- Confidential client, authorization code flow with PKCE S256, exact redirect URI,
  `state` bound to a short-lived cookie, refresh token rotation with revocation.

## V11 Cryptography: Partial

- Met: only standard primitives from vetted libraries: `secrets` for every random value,
  sha256 for stored tokens (pairing codes, invitations, session ids), HMAC-SHA256 for
  webhook signatures with constant-time comparison, TLS for MQTT; device passwords
  hashed by the broker's dynamic-security plugin.
- Gap (R2): webhook secrets (Postgres) and OAuth tokens (Redis) are stored in clear,
  because the hub must use them. Planned: envelope encryption with an external key.

## V12 Secure Communication: Partial

- Met: MQTT is TLS-only with a dedicated CA; no anonymous listener.
- Gap (R4): the development HTTP edge is plain `http://localhost`; production must
  terminate TLS in front of the BFF (HSTS is already sent). Internal traffic between
  containers is unencrypted on the private Docker network.

## V13 Configuration: Partial

- Met: images run as a non-root user with a read-only filesystem, no capabilities and
  `no-new-privileges`; published ports bind to `127.0.0.1` except MQTT; Redis requires a
  password; secrets come from the environment (`.env`, never committed); dependency and
  image scanning (`pip-audit`, `npm audit`, Trivy) on every PR; pinned image versions.
- Gap (R1): the applications connect to Postgres as the database owner.

## V14 Data Protection: Met

- Per-user data is never cached by intermediaries (`no-store`).
- Webhooks carry the alert, not the home's telemetry; audit entries record a webhook's
  host, never its secret; httpx no longer logs outbound URLs.
- Retention: raw telemetry 30 days, aggregates 90 days / 2 years, resolved alerts and
  notifications 90 days, delivered outbox messages 24 h.

## V15 Secure Coding and Architecture: Met

- Module boundaries enforced by import-linter; the domain imports no frameworks.
- Concurrency: every lock or uniqueness guarantee has a test that fails without it
  (ADR 0015).
- Dependencies are locked (Poetry, npm) and scanned; generated code (OpenAPI types) is
  checked for drift.

## V16 Security Logging and Error Handling: Met

- Structured JSON logs with trace and span ids, shipped to Loki; values never
  interpolated into messages.
- Security-relevant events (homes created, invitations and memberships, critical
  commands, webhook and tariff changes) go to a per-home, hash-chained, append-only audit log with a
  verification endpoint. Sign-ins are recorded by Keycloak's event log, not by the hub.
- Operational alerting on failures that hide attacks or outages (ingestion stalled,
  dead-lettered events, delivery failures, 5xx rate), unit-tested with promtool.
- Errors never leak internals: problem details, generic 404 for foreign resources.
