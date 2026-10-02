// Minimal BFF client. The browser holds only an HttpOnly session cookie; unsafe requests
// echo the CSRF token the BFF hands out with the session.

export interface SessionInfo {
  user: { id: string; email: string | null; name: string | null };
  authenticated_at: string;
  csrf_token: string;
}

export type Role = "owner" | "resident" | "guest" | "viewer";

export interface Home {
  id: string;
  name: string;
  timezone: string;
  role: Role;
  access_expires_at: string | null;
}

const BASE = "/api";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { credentials: "same-origin", ...init });
  if (!response.ok) {
    throw new ApiError(response.status, await response.text());
  }
  return (await response.json()) as T;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    body: string,
  ) {
    super(`HTTP ${String(status)}: ${body}`);
  }
}

export async function getSession(): Promise<SessionInfo | null> {
  try {
    return await request<SessionInfo>("/auth/session");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export function loginUrl(returnTo: string): string {
  return `${BASE}/auth/login?${new URLSearchParams({ return_to: returnTo }).toString()}`;
}

export function listHomes(): Promise<Home[]> {
  return request<Home[]>("/homes");
}

export function createHome(session: SessionInfo, name: string): Promise<Home> {
  return request<Home>("/homes", {
    method: "POST",
    headers: { "content-type": "application/json", "x-csrf-token": session.csrf_token },
    body: JSON.stringify({ name }),
  });
}

export async function logout(session: SessionInfo): Promise<string> {
  const { logout_url } = await request<{ logout_url: string }>("/auth/logout", {
    method: "POST",
    headers: { "x-csrf-token": session.csrf_token },
  });
  return logout_url;
}
