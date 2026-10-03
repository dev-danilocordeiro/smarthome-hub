import { device } from "../test/fakes";
import { applyLive, EMPTY, type LiveState } from "./model";

const AT = "2026-10-03T12:00:00Z";

function withHall(): LiveState {
  return applyLive(EMPTY, { type: "snapshot", devices: [device({ online: false })] });
}

describe("applyLive", () => {
  it("replaces the devices with each snapshot", () => {
    const state = applyLive(withHall(), { type: "snapshot", devices: [device({ id: "plug-1" })] });

    expect(Object.keys(state.devices)).toEqual(["plug-1"]);
  });

  it("takes the full reported state from a state message, which also proves presence", () => {
    const state = applyLive(withHall(), { type: "state", device_id: "light-1", at: AT, on: true });

    expect(state.devices["light-1"]?.reported).toEqual({ on: true });
    expect(state.devices["light-1"]?.online).toBe(true);
  });

  it("merges telemetry into the latest readings", () => {
    let state = applyLive(withHall(), { type: "telemetry", device_id: "light-1", at: AT, power_w: 7 });
    state = applyLive(state, { type: "telemetry", device_id: "light-1", at: AT, rssi_dbm: -60 });

    expect(state.devices["light-1"]?.readings).toEqual({
      power_w: { value: 7, at: AT },
      rssi_dbm: { value: -60, at: AT },
    });
  });

  it("applies presence", () => {
    const state = applyLive(withHall(), { type: "presence", device_id: "light-1", at: AT, online: true });

    expect(state.devices["light-1"]?.online).toBe(true);
  });

  it("records command outcomes by command id", () => {
    const state = applyLive(withHall(), {
      type: "command",
      device_id: "light-1",
      at: AT,
      command_id: "c1",
      status: "acknowledged",
      reason: null,
    });

    expect(state.commands["c1"]).toEqual({ deviceId: "light-1", status: "acknowledged", reason: null });
  });

  it("ignores events for devices it was not given in the snapshot", () => {
    const before = withHall();

    expect(applyLive(before, { type: "state", device_id: "lock-9", at: AT, locked: false })).toBe(before);
  });
});

describe("alert messages", () => {
  it("bump a counter and leave devices alone", () => {
    const state = applyLive(EMPTY, { type: "snapshot", devices: [device()] });
    const next = applyLive(state, {
      type: "alert",
      device_id: "light-1",
      at: "2026-10-03T10:00:00Z",
      alert_id: "a1",
      kind: "device_offline",
      severity: "warning",
      status: "open",
      title: "Hall light is offline",
    });
    expect(next.alerts).toBe(1);
    expect(next.devices).toBe(state.devices);
  });
});
