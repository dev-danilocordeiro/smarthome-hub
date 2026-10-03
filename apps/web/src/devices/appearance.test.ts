import { device } from "../test/fakes";
import { appearance, quickAction } from "./appearance";

describe("device appearance", () => {
  it("shows a light's brightness when it is on", () => {
    expect(appearance(device({ reported: { on: true, brightness_pct: 40 } }))).toEqual({
      tone: "active",
      detail: "40%",
    });
  });

  it("flags an unlocked lock and an open door", () => {
    expect(appearance(device({ kind: "lock", reported: { locked: false } })).tone).toBe("warn");
    expect(
      appearance(
        device({ kind: "contact_sensor", reported: {}, readings: { contact_open: { value: true, at: "" } } }),
      ),
    ).toEqual({ tone: "warn", detail: "open" });
  });

  it("greys out offline and quarantined devices whatever they last reported", () => {
    expect(appearance(device({ online: false, reported: { on: true } })).tone).toBe("offline");
    expect(appearance(device({ status: "quarantined" }))).toEqual({ tone: "offline", detail: "quarantined" });
  });

  it("reads sensors from their latest readings", () => {
    const climate = device({ kind: "climate_sensor", readings: { temperature_c: { value: 21.46, at: "" } } });

    expect(appearance(climate).detail).toBe("21.5°C");
  });
});

describe("quick actions", () => {
  it("toggle lights, plugs, locks and cameras", () => {
    expect(quickAction(device({ reported: { on: true } }))).toEqual({ label: "Turn off", desired: { on: false } });
    expect(quickAction(device({ kind: "lock", reported: { locked: true } }))).toEqual({
      label: "Unlock",
      desired: { locked: false },
    });
    expect(quickAction(device({ kind: "camera", reported: {} }))?.desired).toEqual({ armed: true });
  });

  it("are not offered for sensors or quarantined devices", () => {
    expect(quickAction(device({ kind: "motion_sensor" }))).toBeNull();
    expect(quickAction(device({ status: "quarantined" }))).toBeNull();
  });
});
