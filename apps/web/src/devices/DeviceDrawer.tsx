import { useEffect, useState } from "react";
import { api, type Command, describe, unwrap } from "../api/client";
import { HistoryChart } from "../charts/HistoryChart";
import type { Point } from "../charts/scale";
import type { CommandUpdate, LiveDevice } from "../live/model";
import { appearance, KIND_LABEL, quickAction } from "./appearance";
import type { CommandFeedback } from "./commands";

const RANGES = { "6 h": 6, "24 h": 24, "7 days": 24 * 7 } as const;
type RangeLabel = keyof typeof RANGES;

const UNITS: Record<string, string> = {
  temperature_c: "°C",
  humidity_pct: "%",
  illuminance_lux: "lx",
  power_w: "W",
  energy_wh_total: "Wh",
  voltage_v: "V",
  battery_pct: "%",
  rssi_dbm: "dBm",
  motion: "(share of time)",
  contact_open: "(share of time)",
};

function History({ homeId, device }: { homeId: string; device: LiveDevice }) {
  const metrics = Object.keys(device.readings).sort();
  const [metric, setMetric] = useState(metrics[0] ?? "");
  const [range, setRange] = useState<RangeLabel>("24 h");
  const [points, setPoints] = useState<Point[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!metric) return;
    let cancelled = false;
    const from = new Date(Date.now() - RANGES[range] * 3_600_000).toISOString();
    unwrap(
      api.GET("/homes/{home_id}/devices/{device_id}/telemetry", {
        params: { path: { home_id: homeId, device_id: device.id }, query: { metric, from } },
      }),
    ).then(
      (series) => {
        if (!cancelled) {
          setPoints(series.points);
          setError(null);
        }
      },
      (failure: unknown) => {
        if (!cancelled) setError(describe(failure));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [homeId, device.id, metric, range]);

  if (metrics.length === 0) return null;
  return (
    <section>
      <h3>History</h3>
      <div className="row">
        <label>
          Metric{" "}
          <select value={metric} onChange={(e) => { setMetric(e.target.value); }}>
            {metrics.map((m) => (
              <option key={m}>{m}</option>
            ))}
          </select>
        </label>
        <label>
          Period{" "}
          <select value={range} onChange={(e) => { setRange(e.target.value as RangeLabel); }}>
            {Object.keys(RANGES).map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </label>
      </div>
      {error && <p role="alert">{error}</p>}
      {points && <HistoryChart points={points} unit={UNITS[metric] ?? ""} label={`${metric} over ${range}`} />}
    </section>
  );
}

function RecentCommands({
  homeId,
  deviceId,
  updates,
}: {
  homeId: string;
  deviceId: string;
  updates: Record<string, CommandUpdate>;
}) {
  const [commands, setCommands] = useState<Command[]>([]);
  const latestUpdate = Object.entries(updates).filter(([, u]) => u.deviceId === deviceId).length;

  useEffect(() => {
    let cancelled = false;
    void unwrap(
      api.GET("/homes/{home_id}/devices/{device_id}/commands", {
        params: { path: { home_id: homeId, device_id: deviceId }, query: { limit: 5 } },
      }),
    ).then((found) => {
      if (!cancelled) setCommands(found);
    }, () => undefined);
    return () => {
      cancelled = true;
    };
  }, [homeId, deviceId, latestUpdate]);

  if (commands.length === 0) return null;
  return (
    <section>
      <h3>Recent commands</h3>
      <ul className="plain">
        {commands.map((c) => (
          <li key={c.id}>
            <span className={`badge status-${updates[c.id]?.status ?? c.status}`}>
              {updates[c.id]?.status ?? c.status}
            </span>{" "}
            {c.desired ? JSON.stringify(c.desired) : c.action}{" "}
            <small className="muted">
              by {c.issued_by.startsWith("automation:") ? "an automation" : c.issued_by} ·{" "}
              {new Date(c.issued_at).toLocaleTimeString()}
            </small>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function DeviceDrawer({
  homeId,
  device,
  pending,
  feedback,
  updates,
  canControl,
  onCommand,
  onClose,
}: {
  homeId: string;
  device: LiveDevice;
  pending: boolean;
  feedback: CommandFeedback | null;
  updates: Record<string, CommandUpdate>;
  canControl: boolean;
  onCommand: (desired: Record<string, unknown>) => void;
  onClose: () => void;
}) {
  const look = appearance(device);
  const action = canControl ? quickAction(device) : null;
  const brightness = device.reported["brightness_pct"];
  const [level, setLevel] = useState(typeof brightness === "number" ? brightness : 100);

  return (
    <aside className="drawer" aria-label={`${device.name} details`}>
      <header>
        <div>
          <h2>{device.name}</h2>
          <p className="muted">
            {KIND_LABEL[device.kind]} · {device.room ?? "no room"} ·{" "}
            <span className={`tone-text-${look.tone}`}>{look.detail}</span>
          </p>
        </div>
        <button type="button" className="ghost" onClick={onClose} aria-label="Close details">
          ✕
        </button>
      </header>

      {action && (
        <div className="row">
          <button
            type="button"
            disabled={pending}
            onClick={() => {
              onCommand(action.desired);
            }}
          >
            {pending ? "Sending…" : action.label}
          </button>
        </div>
      )}
      {canControl && device.kind === "light" && device.status === "active" && (
        <label className="row">
          Brightness {level}%
          <input
            type="range"
            min={0}
            max={100}
            step={5}
            value={level}
            onChange={(e) => {
              setLevel(Number(e.target.value));
            }}
            onPointerUp={() => {
              onCommand({ on: level > 0, brightness_pct: level });
            }}
            onKeyUp={() => {
              onCommand({ on: level > 0, brightness_pct: level });
            }}
          />
        </label>
      )}
      {feedback?.kind === "reauth" && feedback.href && (
        <p role="alert">
          {feedback.message}. <a href={feedback.href}>Sign in again</a> and retry.
        </p>
      )}
      {feedback?.kind === "error" && <p role="alert">{feedback.message}</p>}

      {Object.keys(device.reported).length > 0 && (
        <section>
          <h3>Reported state</h3>
          <dl className="pairs">
            {Object.entries(device.reported).map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>{String(value)}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      {Object.keys(device.readings).length > 0 && (
        <section>
          <h3>Latest readings</h3>
          <dl className="pairs">
            {Object.entries(device.readings).map(([metric, reading]) => (
              <div key={metric}>
                <dt>{metric}</dt>
                <dd>
                  {typeof reading.value === "number" ? reading.value.toFixed(1) : String(reading.value)}{" "}
                  {UNITS[metric] ?? ""}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      <History key={device.id} homeId={homeId} device={device} />
      <RecentCommands homeId={homeId} deviceId={device.id} updates={updates} />
    </aside>
  );
}
