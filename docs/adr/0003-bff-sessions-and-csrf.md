---
status: accepted
date: 2026-10-02
---

# 0003. Backend-for-frontend with server-side sessions; no tokens in the browser

## Context and problem statement

The web app (a PWA) must authenticate residents against Keycloak with OpenID Connect,
and the same identity has to protect REST calls, the live WebSocket and critical
commands (unlocking a door). Where do tokens live, and how is the browser's session
protected?

## Decision drivers

- An XSS bug must not be enough to steal credentials that work outside the browser.
- Logout and revocation must be effective server-side, within minutes.
- Critical actions need proof of a *recent* authentication, not just a valid session.
- The PWA should not ship an OAuth library or deal with token refresh.

## Considered options

1. **SPA as a public OIDC client**: tokens in memory or storage, sent as bearer headers.
2. **BFF**: the API is a confidential client, keeps tokens server-side and gives the
   browser an opaque session cookie.
3. A separate BFF gateway service in front of the API.

## Decision outcome

Chosen option: **2, a BFF inside the API process** (`modules/identity/api/bff.py`).

**Login.** `GET /auth/login` starts authorization code + PKCE (S256), with `state`
and `nonce` per attempt. The transaction (verifier, nonce, return path) is stored in
Redis for 10 minutes and taken exactly once. The `state` is also put in a
`__Host-smarthome_login` cookie. The callback must present the same `state` in both
places, which stops login CSRF and a callback replayed into another browser. The
return path accepts only same-site relative paths.

**Callback.** The ID token is validated against the provider's JWKS:
- issuer, audience, `azp`, expiry, nonce;
- algorithm allow-list (no `HS*`, so no algorithm confusion);
- a rate-limited JWKS refetch on an unknown `kid`, which handles key rotation without
  becoming an amplifier.

A fresh random session id is issued every time, which rules out session fixation.

**The session cookie** is `__Host-smarthome_session`: `HttpOnly; Secure;
SameSite=Strict; Path=/`, without `Max-Age`. The browser drops it on close and the
server enforces the lifetime. The `__Host-` prefix stops sibling subdomains from
setting or shadowing it.

**Server-side session.** It lives in Redis under `sha256(session id)`, so a Redis dump
does not contain usable cookies. It holds the tokens, the user, `auth_time` and a CSRF
token, with an absolute lifetime of 12 h (configurable).

**Refresh.** When the 5-minute access token is about to expire, the BFF refreshes it.
Keycloak rotates refresh tokens and rejects a reused one (`revokeRefreshToken`,
`refreshTokenMaxReuse: 0`), so a stolen refresh token is single-use at best. If a
refresh is rejected, the session is deleted. As a result, disabling a user at the IdP
takes effect within one access-token lifetime.

**Parallel refresh.** A per-session Redis lock (`SET NX PX` plus compare-and-delete
release) makes exactly one request refresh; the others wait for its result. Without
it, two tabs would both spend the same refresh token, and rotation would treat the
second use as theft. This is covered by a concurrency test that fails when the lock is
removed.

**Fail closed.** If the IdP is unreachable when a refresh is due, requests get `503`
rather than running on an unverifiable session.

**CSRF.** Unsafe methods need `X-CSRF-Token` equal to the session's token
(constant-time compare). They also need, when the browser sends one, an `Origin` that
is either the web app or the BFF. `SameSite=Strict` already blocks cross-site
requests; the token covers same-site attackers (other ports, subdomains) and older
browsers.

**Re-authentication.** `GET /auth/login?reauth=true` sends `prompt=login&max_age=0`.
The callback checks that `auth_time` is after the request, and `Principal` exposes
`authenticated_within(window)` for critical commands (phase 7).

**Logout.** `POST /auth/logout` (CSRF-protected) deletes the session and returns
Keycloak's end-session URL with `id_token_hint`. The SPA navigates there, which also
ends the SSO session.

**Passkeys** are a realm concern: WebAuthn passwordless policy with discoverable
credentials and user verification required, plus Keycloak 26's passkey support. The
BFF does not change: a passkey login is still a code flow.

### A detail worth knowing: why the callback does not 302

The callback is reached through a redirect chain that started on the identity
provider, which is cross-site in production. Browsers withhold `SameSite=Strict`
cookies on such chains, so a `302` from the callback to the app would arrive without
the session cookie that was just set, and the user would look signed out until the
next click. Instead, the callback returns a tiny HTML page that navigates with
`<meta http-equiv="refresh">`. That is a new same-site navigation, so the cookie goes
along.

### Consequences

- Good: no token ever reaches JavaScript. The E2E test asserts that no JWT appears
  in any app response or cookie.
- Good: revocation, logout and step-up live in one place, and the PWA stays simple.
- Good: the WebSocket (phase 9) authenticates with the same cookie.
- Bad: the API is stateful (Redis). Acceptable: Redis is already required for live
  state and pub/sub.
- Bad: the web app and the BFF must share an origin (Vite proxy in dev, reverse proxy
  in production). This is also what makes `SameSite=Strict` workable.

### Confirmation

- `tests/integration/identity/test_keycloak_login.py` runs against a real Keycloak
  (Testcontainers) with the project's realm export. It covers:
  - the cookie flags, and that no token reaches the browser;
  - CSRF enforcement;
  - a callback delivered to another browser, and a replayed callback;
  - re-authentication against a live SSO session;
  - refresh-token reuse rejection;
  - logout.
- `tests/integration/identity/test_sessions_redis.py`: one refresh for parallel
  requests, a rejected refresh ends the session, absolute lifetime, hashed keys.
- `tests/unit/identity/test_oidc.py`: PKCE, the ID-token validation matrix
  (audience, issuer, nonce, expiry, `azp`, foreign key, HS256 confusion), key
  rotation, and error classification.

## Pros and cons of the options

### 1. Public SPA client

- Good: stateless API.
- Bad: tokens are reachable by any script running in the page. Refresh tokens in the
  browser need extra mitigations (rotation, sender-constraining) that are still
  weaker than not having them there.
- Bad: every client re-implements refresh, and revocation waits for token expiry.

### 3. Separate BFF gateway

- Good: the API stays stateless and bearer-only.
- Bad: one more service and network hop, for no benefit at this size. The BFF code is
  isolated in `identity/api/bff.py` and could move out later.
