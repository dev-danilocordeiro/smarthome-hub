import { device } from "../test/fakes";
import { layout, roomLabel, roomsOf, shortName, UNASSIGNED } from "./layout";

const devices = [
  device({ id: "a", name: "Lamp", room: "Living room" }),
  device({ id: "b", name: "Ceiling", room: "Living room" }),
  device({ id: "c", name: "Sensor", room: null }),
  device({ id: "d", name: "Lock", room: "Entrance" }),
];

describe("floor plan layout", () => {
  it("groups devices by room, sorted, with unassigned devices last", () => {
    expect(roomsOf(devices).map(([name, list]) => [name, list.map((d) => d.name)])).toEqual([
      ["Entrance", ["Lock"]],
      ["Living room", ["Ceiling", "Lamp"]],
      [UNASSIGNED, ["Sensor"]],
    ]);
  });

  it("places every device inside its room and rooms without overlapping", () => {
    const plan = layout(devices, 900);

    for (const room of plan.rooms) {
      for (const placed of room.devices) {
        expect(placed.x).toBeGreaterThan(room.x);
        expect(placed.x).toBeLessThan(room.x + room.width);
        expect(placed.y).toBeGreaterThan(room.y);
        expect(placed.y).toBeLessThan(room.y + room.height);
      }
    }
    for (const a of plan.rooms) {
      for (const b of plan.rooms) {
        if (a === b) continue;
        const apart =
          a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y;
        expect(apart).toBe(true);
      }
    }
    expect(plan.height).toBeGreaterThan(Math.max(...plan.rooms.map((r) => r.y + r.height)));
  });

  it("stacks rooms in one column on a narrow screen", () => {
    const plan = layout(devices, 360);

    expect(new Set(plan.rooms.map((r) => r.x)).size).toBe(1);
  });
});

describe("labels", () => {
  it("make room names readable", () => {
    expect(roomLabel("living_room")).toBe("Living room");
    expect(roomLabel("Entrance")).toBe("Entrance");
  });

  it("drop the room from a device's name and keep it short", () => {
    expect(shortName("Living Room light", "living_room")).toBe("Light");
    expect(shortName("Kitchen plug", "kitchen")).toBe("Plug");
    expect(shortName("Desk lamp", "office")).toBe("Desk lamp");
    expect(shortName("Utility energy meter", "garage")).toBe("Utility ener…");
  });
});
