import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { ALERTS_CHANGED } from "../live/useLiveHome";
import { Bell } from "../notifications/Bell";
import { routeFetch } from "../test/fakes";
import { AlertsPage } from "./AlertsPage";

afterEach(() => {
  vi.unstubAllGlobals();
});

const ALERT = {
  id: "a1",
  kind: "device_offline",
  severity: "critical",
  status: "open",
  device_id: "lock-1",
  title: "Front door is offline",
  details: {},
  opened_at: "2026-10-03T10:00:00Z",
  resolved_at: null,
  acknowledged_by: null,
  acknowledged_at: null,
};
const PREFS = { email_enabled: true, min_email_severity: "warning", quiet_start: null, quiet_end: null };

describe("Alerts page", () => {
  it("lists open alerts and lets a member say they are on it", async () => {
    let acknowledged = false;
    const calls = routeFetch({
      "GET /api/homes/h1/alerts": () => ({
        status: 200,
        body: [acknowledged ? { ...ALERT, acknowledged_by: "u1" } : ALERT],
      }),
      "POST /api/homes/h1/alerts/a1/acknowledge": () => {
        acknowledged = true;
        return { status: 200, body: { ...ALERT, acknowledged_by: "u1" } };
      },
      "GET /api/homes/h1/notification-preferences": { status: 200, body: PREFS },
    });
    render(<AlertsPage homeId="h1" version={0} isOwner={false} isGuest={false} />);

    expect(await screen.findByText("Front door is offline")).toBeInTheDocument();
    expect(screen.getByText("critical")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "I'm on it" }));
    expect(await screen.findByText(/u1 is on it/)).toBeInTheDocument();
    expect(calls.filter((c) => c.path.startsWith("/api/homes/h1/alerts?")).at(0)?.path).toContain("status=open");
  });

  it("saves quiet hours with the member's preferences", async () => {
    const calls = routeFetch({
      "GET /api/homes/h1/alerts": { status: 200, body: [] },
      "GET /api/homes/h1/notification-preferences": { status: 200, body: PREFS },
      "PUT /api/homes/h1/notification-preferences": { status: 200, body: PREFS },
    });
    render(<AlertsPage homeId="h1" version={0} isOwner={false} isGuest={false} />);

    expect(await screen.findByText("Nothing needs attention.")).toBeInTheDocument();
    await userEvent.click(await screen.findByLabelText(/Quiet hours/));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ ...PREFS, quiet_start: 22, quiet_end: 7 });
  });
});

describe("Notification bell", () => {
  it("shows the unread count, refreshes when an alert arrives and marks all read", async () => {
    let unread = 1;
    const calls = routeFetch({
      "GET /api/me/notifications": () => ({
        status: 200,
        body: {
          unread,
          items: [
            {
              id: "n1",
              home_id: "h1",
              alert_id: "a1",
              event: "opened",
              severity: "critical",
              title: "Front door is offline",
              body: "",
              created_at: "2026-10-03T10:00:00Z",
              read_at: unread ? null : "2026-10-03T10:05:00Z",
            },
          ],
        },
      }),
      "POST /api/me/notifications/read-all": () => {
        unread = 0;
        return { status: 200, body: { marked: 1 } };
      },
    });
    render(
      <MemoryRouter>
        <Bell />
      </MemoryRouter>,
    );

    const button = await screen.findByRole("button", { name: "Notifications, 1 unread" });
    unread = 2;
    act(() => {
      window.dispatchEvent(new CustomEvent(ALERTS_CHANGED));
    });
    expect(await screen.findByRole("button", { name: "Notifications, 2 unread" })).toBe(button);

    await userEvent.click(button);
    expect(screen.getByRole("link", { name: /Front door is offline/ })).toHaveAttribute("href", "/homes/h1/alerts");
    await userEvent.click(screen.getByRole("button", { name: "Mark all read" }));
    expect(await screen.findByRole("button", { name: "Notifications, 0 unread" })).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "GET")).toHaveLength(3);
  });
});
