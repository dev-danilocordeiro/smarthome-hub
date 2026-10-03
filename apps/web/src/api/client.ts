// Typed BFF client. Paths and payloads come from the API's OpenAPI document
// (@smarthome/contracts), so a renamed route or field fails `tsc`, not production.
// The browser holds only an HttpOnly session cookie; unsafe requests echo the CSRF
// token the BFF hands out with the session.

import type { components, paths } from "@smarthome/contracts";
import createClient, { type Middleware } from "openapi-fetch";

export type Schemas = components["schemas"];
export type Home = Schemas["HomeOut"];
export type Session = Schemas["SessionOut"];
export type Device = Schemas["DeviceOut"];
export type DeviceKind = Schemas["DeviceKind"];
export type Automation = Schemas["AutomationOut"];
export type Scene = Schemas["SceneOut"];
export type Run = Schemas["RunOut"];
export type DryRun = Schemas["DryRunOut"];
export type Command = Schemas["CommandOut"];

export const BASE = "/api";
const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

const csrf: Middleware = {
  onRequest({ request }) {
    if (csrfToken !== null && UNSAFE.has(request.method)) {
      request.headers.set("x-csrf-token", csrfToken);
    }
    return request;
  },
};

export const api = createClient<paths>({
  // Absolute, because `Request` needs one outside a browser (tests).
  baseUrl: `${window.location.origin}${BASE}`,
  credentials: "same-origin",
  // Looked up per request, so a test can stub `fetch` after this module loads.
  fetch: (request: Request) => globalThis.fetch(request),
});
api.use(csrf);

/** RFC 9457 problem details, as every API error is shaped. */
export interface Problem {
  type: string;
  title: string;
  status: number;
  detail?: string;
  /** Invalid automation: where in the submitted JSON (e.g. "triggers/0/value"). */
  path?: string;
  /** Re-authentication required: where to sign in again. */
  login_url?: string;
}

export const REAUTHENTICATION_REQUIRED = "urn:smarthome:problem:reauthentication-required";

export class ApiProblem extends Error {
  readonly problem: Problem;

  constructor(problem: Problem) {
    super(problem.detail ? `${problem.title}: ${problem.detail}` : problem.title);
    this.problem = problem;
  }

  get status(): number {
    return this.problem.status;
  }
}

function asProblem(error: unknown, response: Response): Problem {
  if (typeof error === "object" && error !== null && "title" in error) {
    return error as Problem;
  }
  // FastAPI's own 422 (request shape) or a non-JSON body.
  const detail = typeof error === "string" ? error : JSON.stringify(error);
  return { type: "about:blank", title: response.statusText || "Request failed", status: response.status, detail };
}

/** The response body, or an `ApiProblem` for any non-2xx answer. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok) {
    throw new ApiProblem(asProblem(error, response));
  }
  return data as T;
}

export function loginUrl(returnTo: string, base = `${BASE}/auth/login`): string {
  const separator = base.includes("?") ? "&" : "?";
  return `${base}${separator}${new URLSearchParams({ return_to: returnTo }).toString()}`;
}

/** Where to send someone whose action needs a fresh sign-in (locks, cameras). */
export function reauthenticationUrl(problem: Problem, returnTo: string): string | null {
  if (problem.type !== REAUTHENTICATION_REQUIRED || !problem.login_url) return null;
  return loginUrl(returnTo, problem.login_url);
}

/** A message for any thrown value, fit to show to the user. */
export function describe(error: unknown): string {
  if (error instanceof ApiProblem) return error.problem.detail ?? error.message;
  if (error instanceof Error) return error.message;
  return typeof error === "string" ? error : JSON.stringify(error);
}
