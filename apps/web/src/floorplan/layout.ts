// Lays a home out as a grid of rooms with their devices, from the rooms devices were
// paired into. No floor plan to draw by hand; deterministic, so it is unit-tested.

import type { LiveDevice } from "../live/model";

export const UNASSIGNED = "Unassigned";
const GAP = 16;
const HEADER = 30;
const CELL = 76;
const PADDING = 12;

export interface PlacedDevice {
  device: LiveDevice;
  x: number; // centre
  y: number;
}

export interface Room {
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
  devices: PlacedDevice[];
}

export interface Plan {
  width: number;
  height: number;
  rooms: Room[];
}

export function roomsOf(devices: LiveDevice[]): [string, LiveDevice[]][] {
  const byRoom = new Map<string, LiveDevice[]>();
  for (const device of devices) {
    const name = device.room ?? UNASSIGNED;
    byRoom.set(name, [...(byRoom.get(name) ?? []), device]);
  }
  return [...byRoom.entries()]
    .map(([name, list]): [string, LiveDevice[]] => [
      name,
      [...list].sort((a, b) => a.name.localeCompare(b.name)),
    ])
    .sort(([a], [b]) => {
      if (a === UNASSIGNED) return 1;
      if (b === UNASSIGNED) return -1;
      return a.localeCompare(b);
    });
}

export function layout(devices: LiveDevice[], width: number): Plan {
  const rooms = roomsOf(devices);
  const columns = Math.max(1, Math.min(3, Math.ceil(Math.sqrt(rooms.length)), Math.floor(width / 220)));
  const roomWidth = (width - GAP * (columns + 1)) / columns;
  const perRow = Math.max(1, Math.floor((roomWidth - 2 * PADDING) / CELL));
  const placed: Room[] = [];
  let y = GAP;
  for (let start = 0; start < rooms.length; start += columns) {
    const row = rooms.slice(start, start + columns);
    const height = Math.max(
      ...row.map(([, list]) => HEADER + PADDING + Math.ceil(list.length / perRow) * CELL),
    );
    row.forEach(([name, list], column) => {
      const x = GAP + column * (roomWidth + GAP);
      placed.push({
        name,
        x,
        y,
        width: roomWidth,
        height,
        devices: list.map((device, i) => ({
          device,
          x: x + PADDING + (i % perRow) * CELL + CELL / 2,
          y: y + HEADER + Math.floor(i / perRow) * CELL + CELL / 2 - 4,
        })),
      });
    });
    y += height + GAP;
  }
  return { width, height: Math.max(y, 120), rooms: placed };
}
