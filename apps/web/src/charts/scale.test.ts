import { geometry } from "./scale";

describe("chart geometry", () => {
  it("maps the oldest point to the left, the newest to the right, and inverts y", () => {
    const shape = geometry(
      [
        { time: "2026-10-03T00:00:00Z", avg: 10, min: 10, max: 10 },
        { time: "2026-10-03T01:00:00Z", avg: 20, min: 15, max: 25 },
      ],
      100,
      50,
      0,
    );

    expect(shape?.line).toBe("M0.0 50.0 L100.0 16.7");
    expect(shape?.low).toBe(10);
    expect(shape?.high).toBe(25);
    expect(shape?.band.startsWith("M0.0 50.0 L100.0 0.0")).toBe(true);
  });

  it("has nothing to draw without points, and centres a single point", () => {
    expect(geometry([], 100, 50)).toBeNull();
    expect(geometry([{ time: "2026-10-03T00:00:00Z", avg: 1, min: 1, max: 1 }], 100, 50, 0)?.line).toBe(
      "M50.0 50.0",
    );
  });
});
