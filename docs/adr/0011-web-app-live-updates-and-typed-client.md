---
status: accepted
date: 2026-10-03
---

# 0011. Web app: live updates over a WebSocket, a client typed from OpenAPI, a PWA shell

## Context and problem statement

The web app shows a home as a floor plan that changes while you look at it: a light
someone switched on, a door that opened, a temperature reading, the outcome of the
command you just sent. It also edits automations, activates scenes and charts history.

Questions to settle:

- How do device changes reach the browser, given several API replicas and changes that
  happen in other processes (the ingestor, the worker)?
- How does a long-lived connection stay as safe as the BFF's cookie + CSRF requests
  (ADR 0003)?
- How does the front end stay in step with the API's shapes without hand-written types
  drifting?
- What does "installable / works offline" mean for an app whose data is live by nature?

## Decision drivers

- One source of truth for live state (the Redis read model, ADR 0008) and for events
  (the device event stream, ADR 0010); no second pipeline.
- Any API replica can serve any home's socket.
- A slow or stale client must not cost the server unbounded memory.
- Session expiry and membership revocation apply to open sockets, not only to new requests.
- A renamed field fails the build, not production.

## Considered options

For live updates:

1. Poll the REST API.
2. Server-Sent Events.
3. **A WebSocket per home**, fed by the device event stream.

For types: hand-written TS interfaces; **types generated from the OpenAPI document**; a
full generated SDK.

## Decision outcome

### Live updates: one WebSocket per open home

`GET /homes/{home}/live` (WebSocket). On connect the server sends a **snapshot** of every
device the member may see (configuration from Postgres, presence and reported state from
the Redis read model, latest readings from Redis), then one message per device event:
`state`, `presence`, `telemetry` and `command` (outcomes, which the commands module now
publishes to the same stream when an ack or a timeout settles a command).

- **Fan-out.** Each API process tails `events:devices` once with plain `XREAD` from `$`
  (no consumer group: every replica must see every event) and hands events to an
  in-process `LiveHub` keyed by home. A client can connect to any replica.
- **Back-pressure.** Each socket has a bounded queue (256 messages). A client that falls
  behind is closed with `1013 Try again later` rather than buffered without limit; it
  reconnects and starts from a new snapshot, which is the correct state anyway.
- **Snapshots, not replay.** Events missed while disconnected are not replayed: the
  snapshot on reconnect replaces them. The stream tail likewise drops what it missed
  while Redis was unreachable. Live views converge; history lives in TimescaleDB.
- SSE (option 2) would have done for server-to-client only, but browsers cap concurrent
  HTTP/1.1 connections per origin and the proxying story is the same as for WebSockets.
  Polling (option 1) multiplies load by clients and still lags.

### Security of the socket

- **Cross-site WebSocket hijacking.** A WebSocket handshake carries the session cookie
  but no CSRF header, and the same-origin policy does not apply to it. The handshake is
  accepted only if `Origin` is one of the origins we serve (the web app, the BFF); a
  missing `Origin` is refused too. Browsers always send it on WebSocket handshakes.
- **Authorization** is the same as for HTTP: the session cookie, `view_home` on the home,
  and a guest's device scope (a guest's snapshot and events contain only their devices).
- **Long-lived sessions.** Every 60 s the socket re-checks the session (expiry, logout,
  refresh failure) and the membership (revoked, guest pass expired) and closes with
  `4401` or `4403`. The client stops reconnecting on those codes and asks the person to
  sign in again.
- Clients send nothing that matters; frames from the browser are read and ignored.

### A client typed from OpenAPI

`scripts/export_openapi.py` writes the API's OpenAPI document to
`packages/contracts/openapi.json`; `openapi-typescript` turns it into
`packages/contracts/src/schema.d.ts`; the web app calls the API through `openapi-fetch`
with those types, so paths, parameters and bodies are checked by `tsc`. `make gen-client`
regenerates both; CI fails if either is stale (the backend workflow re-exports and
compares, the web workflow regenerates and diffs). A full SDK generator was more
machinery than a thin fetch wrapper needs. The WebSocket messages are not in OpenAPI;
they are typed next to the reducer that applies them (`apps/web/src/live/model.ts`).

### The front end

- React 19, `react-router`, no state library: the live state is a pure reducer
  (`applyLive`) over socket messages, unit-tested without React.
- The **floor plan** is SVG laid out from the rooms devices were paired into (no drawing
  tool): rooms in a grid, one glyph per device, colour by state, keyboard-accessible.
  Selecting a device opens a panel with its controls, reported state, latest readings,
  a history chart (SVG; the average as a line, min..max as a band, from the
  auto-resolution telemetry endpoint) and its recent commands.
- Commands answer `202`; the device's glyph shows the command in flight until the
  `command` message settles it. A `401 reauthentication-required` (locks, cameras)
  becomes a "sign in again" link that returns to the same page.
- **Automations**: list (status, suspension reason), enable/disable, an editor for the
  JSON DSL with templates filled with the home's devices, server-side validation shown
  with its JSON path, the dry run over the last 24 h, `If-Match` on save, and recent runs.
- **Scenes**: activate, and create one from what selected devices report right now.

### PWA

A manifest and a hand-written service worker (`public/sw.js`, registered in production
builds only). It never touches `/api`: data, sessions and the live socket always go to
the network. Navigations are network-first with the cached shell as the offline
fallback; hashed build assets are cache-first. Offline, the app opens and shows that it
is reconnecting. Push notifications belong to phase 10.

### Consequences

- Good, because the browser sees a device change within the time it takes the ingestor
  to apply it, through the same stream the automation engine uses.
- Good, because any API replica serves any home, and a slow client cannot exhaust memory.
- Good, because an open socket loses access when the session or membership does.
- Good, because API changes that break the front end fail CI.
- Bad, because every API replica reads every event, including homes nobody is watching.
  Fine for a hub; a large fleet would partition the stream by home.
- Bad, because events missed during a disconnect are not replayed (by design: a snapshot
  replaces them).
- Bad, because the floor plan is a grid of rooms, not the real layout of the house.

### Confirmation

- Integration (`tests/integration/live`, real uvicorn + `websockets` client): snapshot then
  events of the member's home only; a guest's scope; refused handshakes (foreign origin,
  no origin, no session, not a member); closing with `4403` when the membership is
  revoked; closing a slow client with `1013`.
- Unit (Vitest): the live reducer, floor plan layout and labels, device appearance and
  quick actions, chart geometry, templates, the client's CSRF and problem handling; and
  component tests with a fake socket: the floor plan following events, a command in
  flight until its outcome, the step-up link for a lock, reconnect with back-off and
  stopping on `4401`, the automation editor's validation path, dry run and `If-Match`.
- Checked live: signed in through Keycloak, opened a simulated home, switched a light
  and saw its state come back over the socket; ran a dry run over the day's telemetry.
- Metrics: `smarthome.live.sockets`, `smarthome.live.dropped`.
