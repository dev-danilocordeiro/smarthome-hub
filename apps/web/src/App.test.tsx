import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";

function respond(routes: Record<string, { status: number; body: unknown }>) {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const key = `${init?.method ?? "GET"} ${url}`;
      const route = routes[key];
      if (!route) return Promise.reject(new Error(`unexpected ${key}`));
      return Promise.resolve(new Response(JSON.stringify(route.body), { status: route.status }));
    }),
  );
  return calls;
}

const session = {
  user: { id: "u1", email: "alice@smarthome.local", name: "Alice Owner" },
  authenticated_at: "2026-10-02T10:00:00Z",
  csrf_token: "csrf-123",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("renders the product name as the page heading", () => {
    respond({ "GET /api/auth/session": { status: 401, body: {} } });
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "Smart Home Hub" })).toBeInTheDocument();
  });

  it("offers a sign-in link when there is no session", async () => {
    respond({ "GET /api/auth/session": { status: 401, body: {} } });
    render(<App />);

    const link = await screen.findByRole("link", { name: "Sign in" });
    expect(link).toHaveAttribute("href", "/api/auth/login?return_to=%2F");
  });

  it("lists the signed-in user's homes", async () => {
    respond({
      "GET /api/auth/session": { status: 200, body: session },
      "GET /api/homes": {
        status: 200,
        body: [{ id: "h1", name: "Casa", timezone: "UTC", role: "owner", access_expires_at: null }],
      },
    });
    render(<App />);

    expect(await screen.findByText("Alice Owner")).toBeInTheDocument();
    expect(screen.getByText("Casa")).toBeInTheDocument();
  });

  it("sends the CSRF token when creating a home", async () => {
    const calls = respond({
      "GET /api/auth/session": { status: 200, body: session },
      "GET /api/homes": { status: 200, body: [] },
      "POST /api/homes": {
        status: 201,
        body: { id: "h2", name: "Praia", timezone: "UTC", role: "owner", access_expires_at: null },
      },
    });
    render(<App />);

    await userEvent.type(await screen.findByLabelText(/New home/), "Praia");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    const post = calls.find((c) => c.init?.method === "POST");
    expect(new Headers(post?.init?.headers).get("x-csrf-token")).toBe("csrf-123");
  });
});
