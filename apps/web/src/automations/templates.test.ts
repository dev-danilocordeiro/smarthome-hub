import { device } from "../test/fakes";
import { parseDefinition, templates } from "./templates";

describe("automation templates", () => {
  it("use the home's real device ids", () => {
    const [motionLight] = templates([
      device({ id: "motion-7", kind: "motion_sensor" }),
      device({ id: "light-3" }),
    ]);

    expect(JSON.stringify(motionLight?.definition)).toContain('"device_id":"motion-7"');
    expect(JSON.stringify(motionLight?.definition)).toContain('"device_id":"light-3"');
  });

  it("leave a visible placeholder when the home lacks a kind", () => {
    const lock = templates([]).find((t) => t.name.includes("Lock"));

    expect(JSON.stringify(lock?.definition)).toContain("<lock id>");
  });
});

describe("parseDefinition", () => {
  it("accepts objects and explains everything else", () => {
    expect(parseDefinition('{"schema_version": "1"}')).toEqual({ value: { schema_version: "1" } });
    expect(parseDefinition("[]")).toEqual({ error: "The definition must be a JSON object." });
    expect(parseDefinition("{oops")).toHaveProperty("error");
  });
});
