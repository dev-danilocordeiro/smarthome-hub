import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { device, routeFetch } from "../test/fakes";
import { AutomationEditor } from "./AutomationEditor";

afterEach(() => {
  vi.unstubAllGlobals();
});

const SAVED = {
  id: "a1",
  name: "Motion turns a light on after dark",
  description: null,
  enabled: true,
  status: "active",
  status_reason: null,
  version: 1,
  definition: {},
  timezone: "America/Sao_Paulo",
  created_by: "u1",
  created_at: "2026-10-03T00:00:00Z",
  updated_by: "u1",
  updated_at: "2026-10-03T00:00:00Z",
  warnings: ["'A', 'B' can trigger each other in a loop"],
};

function renderEditor(automation: typeof SAVED | null = null) {
  const onSaved = vi.fn();
  render(
    <AutomationEditor
      homeId="h1"
      automation={automation as never}
      devices={[device({ id: "motion-1", kind: "motion_sensor", name: "Hall motion" }), device()]}
      onSaved={onSaved}
      onCancel={() => undefined}
    />,
  );
  return onSaved;
}

describe("AutomationEditor", () => {
  it("starts from a template with the home's devices and creates the automation", async () => {
    const calls = routeFetch({ "POST /api/homes/h1/automations": { status: 201, body: SAVED } });
    const onSaved = renderEditor();

    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    const body = calls[0]?.body as { definition: { triggers: { device_id: string }[] } };
    expect(body.definition.triggers[0]?.device_id).toBe("motion-1");
    expect(onSaved).toHaveBeenCalled();
    expect(await screen.findByText(/can trigger each other/)).toBeInTheDocument();
  });

  it("shows where the server found the definition invalid", async () => {
    routeFetch({
      "POST /api/homes/h1/automations": {
        status: 422,
        body: {
          type: "urn:smarthome:problem:invalid-automation",
          title: "Invalid definition",
          status: 422,
          detail: "triggers/0/value: `gt` compares numbers only",
          path: "triggers/0/value",
        },
      },
    });
    renderEditor();

    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Invalid definition");
    expect(alert).toHaveTextContent("at triggers/0/value");
  });

  it("refuses to send text that is not JSON", async () => {
    const calls = routeFetch({});
    renderEditor();
    const area = screen.getByLabelText(/Definition/);

    await userEvent.clear(area);
    await userEvent.type(area, "not json");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Not valid JSON");
    expect(calls).toHaveLength(0);
  });

  it("saves edits with If-Match and runs a dry run", async () => {
    const calls = routeFetch({
      "PUT /api/homes/h1/automations/a1": { status: 200, body: { ...SAVED, version: 2, warnings: [] } },
      "POST /api/homes/h1/automations/dry-run": {
        status: 200,
        body: {
          runs: [{ at: "2026-10-02T21:00:00Z", trigger_index: 0, outcome: "would_run", reason: null, failed_conditions: [], unknown_conditions: [] }],
          warnings: [],
          truncated: false,
        },
      },
    });
    renderEditor({ ...SAVED, definition: { schema_version: "1" } });

    await userEvent.click(screen.getByRole("button", { name: /Dry run/ }));
    expect(await screen.findByText(/Would have run 1 time in/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save version 2" }));
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.headers.get("if-match")).toBe('"1"');
  });
});
