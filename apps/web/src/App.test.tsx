import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { device, FakeSocket, routeFetch, SESSION } from "./test/fakes";

const HOME = { id: "h1", name: "Casa", timezone: "America/Sao_Paulo", role: "owner", access_expires_at: null };

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("App", () => {
  it("offers a sign-in link that returns to the current page", async () => {
    routeFetch({ "GET /api/auth/session": { status: 401, body: { title: "Not signed in", status: 401 } } });
    render(<App />);

    const link = await screen.findByRole("link", { name: "Sign in" });
    expect(link).toHaveAttribute("href", "/api/auth/login?return_to=%2F");
    expect(screen.getByRole("heading", { level: 1, name: "Smart Home Hub" })).toBeInTheDocument();
  });

  it("lists the signed-in user's homes and creates one in the browser's time zone", async () => {
    const calls = routeFetch({
      "GET /api/auth/session": { status: 200, body: SESSION },
      "GET /api/homes": { status: 200, body: [HOME] },
      "POST /api/homes": { status: 201, body: { ...HOME, id: "h2", name: "Praia" } },
    });
    render(<App />);

    expect(await screen.findByText("Alice Owner")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Casa" })).toHaveAttribute("href", "/homes/h1");

    await userEvent.type(screen.getByLabelText(/New home/), "Praia");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    const post = calls.find((c) => c.method === "POST");
    expect(post?.headers.get("x-csrf-token")).toBe("csrf-123");
    expect(post?.body).toEqual({ name: "Praia", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone });
  });
});

describe("Home floor plan", () => {
  function openHome(routes: Parameters<typeof routeFetch>[0] = {}) {
    FakeSocket.install();
    const calls = routeFetch({
      "GET /api/auth/session": { status: 200, body: SESSION },
      "GET /api/homes/h1": { status: 200, body: HOME },
      "GET /api/homes/h1/devices/light-1/commands": { status: 200, body: [] },
      ...routes,
    });
    window.history.pushState({}, "", "/homes/h1");
    render(<App />);
    return calls;
  }

  it("draws the snapshot and follows live events", async () => {
    openHome();
    await screen.findByText("Connecting…");
    const socket = FakeSocket.last();
    expect(socket.url).toBe(`ws://${window.location.host}/api/homes/h1/live`);

    socket.receive({ type: "snapshot", devices: [device(), device({ id: "lock-1", kind: "lock", name: "Front door", room: "Entrance", reported: { locked: true } })] });

    expect(await screen.findByText("Live")).toBeInTheDocument();
    const plan = screen.getByRole("group", { name: "Floor plan" });
    expect(within(plan).getByRole("button", { name: "Hall light, Light: off" })).toBeInTheDocument();
    expect(within(plan).getByText("Entrance")).toBeInTheDocument();

    socket.receive({ type: "state", device_id: "light-1", at: "2026-10-03T12:00:00Z", on: true, brightness_pct: 80 });

    expect(await within(plan).findByRole("button", { name: "Hall light, Light: 80%" })).toBeInTheDocument();
  });

  it("sends a command from the device panel and shows it in flight until the outcome arrives", async () => {
    const calls = openHome({
      "POST /api/homes/h1/devices/light-1/commands": {
        status: 202,
        body: { id: "c1", device_id: "light-1", action: "set_state", status: "pending" },
      },
    });
    const socket = await vi.waitFor(() => FakeSocket.last());
    socket.receive({ type: "snapshot", devices: [device()] });

    await userEvent.click(await screen.findByRole("button", { name: "Hall light, Light: off" }));
    await userEvent.click(screen.getByRole("button", { name: "Turn on" }));

    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ action: "set_state", desired: { on: true } });
    expect(await screen.findByRole("button", { name: "Sending…" })).toBeDisabled();

    socket.receive({ type: "command", device_id: "light-1", at: "", command_id: "c1", status: "acknowledged", reason: null });
    socket.receive({ type: "state", device_id: "light-1", at: "2026-10-03T12:00:01Z", on: true });

    expect(await screen.findByRole("button", { name: "Turn off" })).toBeEnabled();
  });

  it("asks for a fresh sign-in when a lock needs one", async () => {
    openHome({
      "GET /api/homes/h1/devices/lock-1/commands": { status: 200, body: [] },
      "POST /api/homes/h1/devices/lock-1/commands": {
        status: 401,
        body: {
          type: "urn:smarthome:problem:reauthentication-required",
          title: "Sign in again to operate locks and cameras",
          status: 401,
          login_url: "/api/auth/login?reauth=true",
        },
      },
    });
    const socket = await vi.waitFor(() => FakeSocket.last());
    socket.receive({ type: "snapshot", devices: [device({ id: "lock-1", kind: "lock", name: "Front door", reported: { locked: true } })] });

    await userEvent.click(await screen.findByRole("button", { name: "Front door, Lock: locked" }));
    await userEvent.click(screen.getByRole("button", { name: "Unlock" }));

    expect(await screen.findByRole("link", { name: "Sign in again" })).toHaveAttribute(
      "href",
      "/api/auth/login?reauth=true&return_to=%2Fhomes%2Fh1",
    );
  });

  it("reconnects after the socket drops and stops when the session is gone", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    openHome();
    const first = await vi.waitFor(() => FakeSocket.last());

    first.drop(1006);
    expect(await screen.findByText("Reconnecting…")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(2);

    FakeSocket.last().drop(4401);
    expect(await screen.findByText("Signed out")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.useRealTimers();
  });

  it("says so, and stops retrying, when the person is not a member of the home", async () => {
    openHome({ "GET /api/homes/h1": { status: 404, body: { title: "Home not found", status: 404 } } });
    await screen.findByText("Connecting…");
    FakeSocket.last().drop(4403);

    expect(await screen.findByRole("alert")).toHaveTextContent(/not a member/);
    expect(screen.getByRole("status")).toHaveTextContent("No access");
    expect(screen.queryByRole("group", { name: "Floor plan" })).not.toBeInTheDocument();
    expect(FakeSocket.instances).toHaveLength(1);
  });
});
