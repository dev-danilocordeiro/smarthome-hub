// Test doubles: a routed `fetch` and a controllable `WebSocket`.

import type { LiveDevice, LiveMessage } from "../live/model";

export interface Call {
  method: string;
  path: string;
  headers: Headers;
  body: unknown;
}

type Route = { status: number; body: unknown } | ((call: Call) => { status: number; body: unknown });

export function routeFetch(routes: Record<string, Route>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const url = new URL(input.url);
      const text = await input.text();
      const call: Call = {
        method: input.method,
        path: url.pathname + url.search,
        headers: input.headers,
        body: text ? (JSON.parse(text) as unknown) : null,
      };
      calls.push(call);
      const route = routes[`${call.method} ${url.pathname}`];
      if (!route) throw new Error(`unexpected ${call.method} ${url.pathname}`);
      const { status, body } = typeof route === "function" ? route(call) : route;
      return new Response(status === 204 ? null : JSON.stringify(body), {
        status,
        headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" },
      });
    }),
  );
  return calls;
}

export class FakeSocket {
  static instances: FakeSocket[] = [];
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  receive(message: LiveMessage): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(message) }));
  }

  drop(code = 1006): void {
    this.onclose?.(new CloseEvent("close", { code }));
  }

  close(): void {
    this.closed = true;
  }

  static install(): void {
    FakeSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeSocket);
  }

  static last(): FakeSocket {
    const socket = FakeSocket.instances.at(-1);
    if (!socket) throw new Error("no WebSocket opened");
    return socket;
  }
}

export function device(overrides: Partial<LiveDevice> = {}): LiveDevice {
  return {
    id: "light-1",
    kind: "light",
    name: "Hall light",
    room: "Hall",
    status: "active",
    online: true,
    reported: { on: false, brightness_pct: 60 },
    readings: {},
    ...overrides,
  };
}

export const SESSION = {
  user: { id: "u1", email: "alice@smarthome.local", name: "Alice Owner" },
  authenticated_at: "2026-10-02T10:00:00Z",
  csrf_token: "csrf-123",
};
