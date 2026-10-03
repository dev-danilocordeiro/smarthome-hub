import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { appearance, KIND_LABEL } from "../devices/appearance";
import type { LiveDevice } from "../live/model";
import { layout } from "./layout";

const FALLBACK_WIDTH = 960;
const RADIUS = 22;

function Icon({ kind }: { kind: LiveDevice["kind"] }) {
  // Simple strokes, drawn around (0, 0) in a 24 px box.
  switch (kind) {
    case "light":
      return <path d="M-5 4 a7 7 0 1 1 10 0 v4 h-10 z M-3 10 h6" />;
    case "plug":
      return <path d="M-6 -2 h12 v6 a6 6 0 0 1 -12 0 z M-3 -2 v-6 M3 -2 v-6 M0 10 v3" />;
    case "lock":
      return <path d="M-7 -1 h14 v10 h-14 z M-4 -1 v-4 a4 4 0 0 1 8 0 v4" />;
    case "camera":
      return <path d="M-9 -5 h12 v10 h-12 z M3 -2 l6 -3 v10 l-6 -3" />;
    case "thermostat":
      return <path d="M-2 -9 h4 v11 a4 4 0 1 1 -4 0 z" />;
    case "energy_meter":
      return <path d="M2 -10 l-8 12 h6 l-2 8 l8 -12 h-6 z" />;
    case "contact_sensor":
      return <path d="M-8 -9 h7 v18 h-7 z M2 -9 h6 v18 h-6 z" />;
    case "motion_sensor":
      return <path d="M-8 0 a8 8 0 0 1 16 0 M-4 0 a4 4 0 0 1 8 0 M0 0 v0.1" />;
    case "climate_sensor":
      return <path d="M-6 6 a6 6 0 0 1 0 -12 a7 7 0 0 1 13 3 a4 4 0 0 1 -1 9 z" />;
  }
}

function DeviceGlyph({
  device,
  x,
  y,
  selected,
  pending,
  onSelect,
}: {
  device: LiveDevice;
  x: number;
  y: number;
  selected: boolean;
  pending: boolean;
  onSelect: (id: string) => void;
}) {
  const look = appearance(device);
  const label = `${device.name}, ${KIND_LABEL[device.kind]}: ${look.detail}`;
  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect(device.id);
    }
  }
  return (
    <g
      className={`device tone-${look.tone}${selected ? " selected" : ""}${pending ? " pending" : ""}`}
      transform={`translate(${String(x)} ${String(y)})`}
      role="button"
      tabIndex={0}
      aria-label={label}
      aria-pressed={selected}
      onClick={() => {
        onSelect(device.id);
      }}
      onKeyDown={onKeyDown}
    >
      <title>{label}</title>
      <circle r={RADIUS} className="halo" />
      <g className="icon">
        <Icon kind={device.kind} />
      </g>
      <text y={RADIUS + 14} className="name">
        {device.name.length > 14 ? `${device.name.slice(0, 13)}…` : device.name}
      </text>
      <text y={RADIUS + 27} className="detail">
        {look.detail}
      </text>
    </g>
  );
}

export function FloorPlan({
  devices,
  selected,
  pending,
  onSelect,
}: {
  devices: LiveDevice[];
  selected: string | null;
  pending: ReadonlySet<string>;
  onSelect: (id: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(FALLBACK_WIDTH);

  useEffect(() => {
    const element = container.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry && entry.contentRect.width > 0) setWidth(Math.round(entry.contentRect.width));
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, []);

  const plan = layout(devices, width);
  return (
    <div ref={container} className="floorplan">
      <svg
        viewBox={`0 0 ${String(plan.width)} ${String(plan.height)}`}
        width="100%"
        role="group"
        aria-label="Floor plan"
      >
        {plan.rooms.map((room) => (
          <g key={room.name} className="room">
            <rect x={room.x} y={room.y} width={room.width} height={room.height} rx={10} />
            <text x={room.x + 12} y={room.y + 20} className="room-name">
              {room.name}
            </text>
            {room.devices.map(({ device, x, y }) => (
              <DeviceGlyph
                key={device.id}
                device={device}
                x={x}
                y={y}
                selected={device.id === selected}
                pending={pending.has(device.id)}
                onSelect={onSelect}
              />
            ))}
          </g>
        ))}
      </svg>
    </div>
  );
}
