// Messages of the live WebSocket (GET /homes/{id}/live). Not part of OpenAPI; this file
// mirrors apps/api/src/smarthome/modules/devices/api/live.py.

import type { DeviceKind } from "../api/client";

export interface Reading {
  value: number | boolean;
  at: string;
}

export interface LiveDevice {
  id: string;
  kind: DeviceKind;
  name: string;
  room: string | null;
  status: string;
  online: boolean;
  reported: Record<string, unknown>;
  readings: Record<string, Reading>;
}

export type LiveMessage =
  | { type: "snapshot"; devices: LiveDevice[] }
  | ({ type: "state"; device_id: string; at: string } & Record<string, unknown>)
  | { type: "presence"; device_id: string; at: string; online: boolean }
  | ({ type: "telemetry"; device_id: string; at: string } & Record<string, unknown>)
  | {
      type: "command";
      device_id: string;
      at: string;
      command_id: string;
      status: string;
      reason: string | null;
    };

export interface CommandUpdate {
  deviceId: string;
  status: string;
  reason: string | null;
}

export interface LiveState {
  devices: Record<string, LiveDevice>;
  commands: Record<string, CommandUpdate>;
}

export const EMPTY: LiveState = { devices: {}, commands: {} };

const ENVELOPE = new Set(["type", "device_id", "at"]);

function payload(message: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(message).filter(([key]) => !ENVELOPE.has(key)));
}

/** Apply one message. Events for devices not in the snapshot (outside a guest's pass,
 * or paired after the snapshot) are ignored until the next snapshot. */
export function applyLive(state: LiveState, message: LiveMessage): LiveState {
  if (message.type === "snapshot") {
    return {
      devices: Object.fromEntries(message.devices.map((d) => [d.id, d])),
      commands: state.commands,
    };
  }
  if (message.type === "command") {
    return {
      ...state,
      commands: {
        ...state.commands,
        [message.command_id]: {
          deviceId: message.device_id,
          status: message.status,
          reason: message.reason,
        },
      },
    };
  }
  const device = state.devices[message.device_id];
  if (!device) return state;
  let updated: LiveDevice;
  if (message.type === "presence") {
    updated = { ...device, online: message.online };
  } else if (message.type === "state") {
    // A state message carries the full reported state; it also proves the device is up.
    updated = { ...device, reported: payload(message), online: true };
  } else {
    const readings = Object.fromEntries(
      Object.entries(payload(message)).map(([metric, value]) => [
        metric,
        { value: value as number | boolean, at: message.at },
      ]),
    );
    updated = { ...device, readings: { ...device.readings, ...readings }, online: true };
  }
  return { ...state, devices: { ...state.devices, [device.id]: updated } };
}
