import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { routeFetch } from "../test/fakes";
import { EnergyPage, kwh, range } from "./EnergyPage";

afterEach(() => {
  vi.unstubAllGlobals();
});

const USAGE = {
  from: "2026-09-27T03:00:00Z",
  to: "2026-10-03T12:00:00Z",
  bucket: "day",
  timezone: "America/Sao_Paulo",
  currency: "BRL",
  measured_by: "meter",
  total_wh: 42_500,
  total_cost: "34.00",
  unmetered_wh: 30_000,
  buckets: [
    { start: "2026-10-02T00:00:00-03:00", wh: 20_000, cost: "16.00" },
    { start: "2026-10-03T00:00:00-03:00", wh: 22_500, cost: "18.00" },
  ],
  devices: [
    { device_id: "meter-1", name: "Meter", room: "utility", whole_home: true, wh: 42_500, cost: "34.00" },
    { device_id: "plug-1", name: "Fridge", room: "kitchen", whole_home: false, wh: 12_500, cost: "10.00" },
  ],
};

const TARIFF = {
  currency: "BRL",
  base_price: "0.800000",
  periods: [],
  monthly_budget_kwh: null,
  timezone: "America/Sao_Paulo",
  version: 3,
  updated_by: "u1",
  updated_at: "2026-10-01T00:00:00Z",
};

describe("Energy page", () => {
  it("shows the total, a bar per day and a per-device breakdown with cost", async () => {
    routeFetch({
      "GET /api/homes/h1/energy/usage": { status: 200, body: USAGE },
      "GET /api/homes/h1/energy/tariff": { status: 200, body: TARIFF },
    });
    render(<EnergyPage homeId="h1" canManage />);

    expect(await screen.findByText(/42[.,]5 kWh/)).toBeInTheDocument();
    expect(screen.getByText(/whole-home meter/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Consumption per day" }).querySelectorAll("rect")).toHaveLength(2);
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("Device"),
      expect.stringContaining("Meter"),
      expect.stringContaining("Fridge"),
      expect.stringContaining("Everything without a plug"),
    ]);
  });

  it("saves the tariff with the version it was loaded at", async () => {
    const calls = routeFetch({
      "GET /api/homes/h1/energy/usage": { status: 200, body: USAGE },
      "GET /api/homes/h1/energy/tariff": { status: 200, body: TARIFF },
      "PUT /api/homes/h1/energy/tariff": { status: 200, body: { ...TARIFF, base_price: "0.912345", version: 4 } },
    });
    render(<EnergyPage homeId="h1" canManage />);

    const price = await screen.findByLabelText(/Price per kWh/);
    await userEvent.clear(price);
    await userEvent.type(price, "0.912345");
    await userEvent.click(screen.getByRole("button", { name: "Save tariff" }));

    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.headers.get("if-match")).toBe('"3"');
    expect(put?.body).toMatchObject({ currency: "BRL", base_price: "0.912345", monthly_budget_kwh: null });
  });

  it("starts an empty tariff when the home has none and says a concurrent edit won", async () => {
    routeFetch({
      "GET /api/homes/h1/energy/usage": { status: 200, body: { ...USAGE, total_wh: 0, devices: [], buckets: [] } },
      "GET /api/homes/h1/energy/tariff": { status: 404, body: { title: "No tariff", status: 404 } },
      "PUT /api/homes/h1/energy/tariff": { status: 412, body: { title: "Edited version is not current", status: 412 } },
    });
    render(<EnergyPage homeId="h1" canManage />);

    expect(await screen.findByText(/No tariff yet/)).toBeInTheDocument();
    expect(screen.getByText(/No consumption recorded/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Price per kWh/), "0.8");
    await userEvent.click(screen.getByRole("button", { name: "Save tariff" }));
    expect(await screen.findByText(/Someone else changed the tariff/)).toBeInTheDocument();
  });
});

describe("energy helpers", () => {
  it("picks hourly buckets for a day and daily ones for longer ranges", () => {
    const now = new Date(2026, 9, 15, 12, 30);
    expect(range("24h", now).bucket).toBe("hour");
    expect(range("month", now).from).toEqual(new Date(2026, 9, 1));
    expect(range("7d", now).from).toEqual(new Date(2026, 9, 9));
  });

  it("formats kilowatt-hours with fewer decimals as they grow", () => {
    expect(kwh(1234)).toMatch(/^1[.,]23$/);
    expect(kwh(123_456)).toMatch(/^123[.,]5$/);
  });
});
