// How a device looks on the floor plan and what a click does. Pure, so it is unit-tested.

import type { LiveDevice } from "../live/model";

export type Tone = "active" | "idle" | "warn" | "offline";

export interface Appearance {
  tone: Tone;
  detail: string;
}

export interface QuickAction {
  label: string;
  desired: Record<string, boolean>;
}

function bool(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function num(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined;
}

function reading(device: LiveDevice, metric: string): unknown {
  return device.readings[metric]?.value;
}

export function appearance(device: LiveDevice): Appearance {
  if (device.status !== "active") return { tone: "offline", detail: device.status };
  if (!device.online) return { tone: "offline", detail: "offline" };
  const r = device.reported;
  switch (device.kind) {
    case "light": {
      const on = bool(r["on"]) ?? false;
      const brightness = num(r["brightness_pct"]);
      return {
        tone: on ? "active" : "idle",
        detail: on ? (brightness === undefined ? "on" : `${String(brightness)}%`) : "off",
      };
    }
    case "plug": {
      const on = bool(r["on"]) ?? false;
      const power = num(reading(device, "power_w"));
      return {
        tone: on ? "active" : "idle",
        detail: on && power !== undefined ? `${power.toFixed(0)} W` : on ? "on" : "off",
      };
    }
    case "lock": {
      const locked = bool(r["locked"]);
      if (locked === undefined) return { tone: "idle", detail: "unknown" };
      return { tone: locked ? "idle" : "warn", detail: locked ? "locked" : "unlocked" };
    }
    case "camera": {
      const armed = bool(r["armed"]) ?? false;
      return { tone: armed ? "active" : "idle", detail: armed ? "armed" : "disarmed" };
    }
    case "thermostat": {
      const target = num(r["target_temp_c"]);
      const mode = typeof r["hvac_mode"] === "string" ? r["hvac_mode"] : "?";
      return {
        tone: mode === "off" ? "idle" : "active",
        detail: target === undefined ? mode : `${mode} ${target.toFixed(1)}°`,
      };
    }
    case "motion_sensor": {
      const motion = Boolean(reading(device, "motion"));
      return { tone: motion ? "active" : "idle", detail: motion ? "motion" : "still" };
    }
    case "contact_sensor": {
      const open = Boolean(reading(device, "contact_open"));
      return { tone: open ? "warn" : "idle", detail: open ? "open" : "closed" };
    }
    case "climate_sensor": {
      const temperature = num(reading(device, "temperature_c"));
      return { tone: "idle", detail: temperature === undefined ? "–" : `${temperature.toFixed(1)}°C` };
    }
    case "energy_meter": {
      const power = num(reading(device, "power_w"));
      return { tone: "idle", detail: power === undefined ? "–" : `${power.toFixed(0)} W` };
    }
  }
}

/** The one-tap command for a device, if it has one. */
export function quickAction(device: LiveDevice): QuickAction | null {
  if (device.status !== "active") return null;
  const r = device.reported;
  switch (device.kind) {
    case "light":
    case "plug": {
      const on = bool(r["on"]) ?? false;
      return { label: on ? "Turn off" : "Turn on", desired: { on: !on } };
    }
    case "lock": {
      const locked = bool(r["locked"]) ?? false;
      return { label: locked ? "Unlock" : "Lock", desired: { locked: !locked } };
    }
    case "camera": {
      const armed = bool(r["armed"]) ?? false;
      return { label: armed ? "Disarm" : "Arm", desired: { armed: !armed } };
    }
    default:
      return null;
  }
}

export const KIND_LABEL: Record<LiveDevice["kind"], string> = {
  light: "Light",
  plug: "Plug",
  thermostat: "Thermostat",
  lock: "Lock",
  motion_sensor: "Motion sensor",
  contact_sensor: "Door/window sensor",
  climate_sensor: "Climate sensor",
  energy_meter: "Energy meter",
  camera: "Camera",
};
