// Starting points for the editor, filled with the home's real device ids.

import type { LiveDevice } from "../live/model";

export interface Template {
  name: string;
  definition: Record<string, unknown>;
}

function first(devices: LiveDevice[], kind: LiveDevice["kind"]): string {
  return devices.find((d) => d.kind === kind)?.id ?? `<${kind} id>`;
}

export function templates(devices: LiveDevice[]): Template[] {
  const motion = first(devices, "motion_sensor");
  const light = first(devices, "light");
  return [
    {
      name: "Motion turns a light on after dark",
      definition: {
        schema_version: "1",
        triggers: [{ type: "telemetry", device_id: motion, metric: "motion", op: "eq", value: true }],
        conditions: [{ type: "time", after: "18:00", before: "06:00" }],
        actions: [
          { type: "command", device_id: light, action: "set_state", desired: { on: true, brightness_pct: 70 } },
        ],
        cooldown_s: 60,
      },
    },
    {
      name: "Light off after 5 minutes without motion",
      definition: {
        schema_version: "1",
        triggers: [
          { type: "telemetry", device_id: motion, metric: "motion", op: "eq", value: false, for_s: 300 },
        ],
        actions: [{ type: "command", device_id: light, action: "set_state", desired: { on: false } }],
      },
    },
    {
      name: "Lock the door at night on weekdays",
      definition: {
        schema_version: "1",
        triggers: [{ type: "schedule", at: "23:00", weekdays: ["mon", "tue", "wed", "thu", "fri"] }],
        actions: [
          { type: "command", device_id: first(devices, "lock"), action: "set_state", desired: { locked: true } },
        ],
      },
    },
    {
      name: "Too hot: switch a fan plug on",
      definition: {
        schema_version: "1",
        triggers: [
          {
            type: "telemetry",
            device_id: first(devices, "climate_sensor"),
            metric: "temperature_c",
            op: "gt",
            value: 26,
            for_s: 120,
          },
        ],
        actions: [{ type: "command", device_id: first(devices, "plug"), action: "set_state", desired: { on: true } }],
      },
    },
  ];
}

export function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

/** Parse the editor's text, with a readable error instead of an exception. */
export function parseDefinition(text: string): { value: Record<string, unknown> } | { error: string } {
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      return { error: "The definition must be a JSON object." };
    }
    return { value: value as Record<string, unknown> };
  } catch (error) {
    return { error: `Not valid JSON: ${error instanceof Error ? error.message : String(error)}` };
  }
}
