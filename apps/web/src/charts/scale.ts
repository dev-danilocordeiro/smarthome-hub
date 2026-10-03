// Turns a telemetry series into SVG paths: the average as a line, min..max as a band.

export interface Point {
  time: string;
  avg: number;
  min: number;
  max: number;
}

export interface ChartGeometry {
  line: string;
  band: string;
  low: number;
  high: number;
}

export function geometry(points: Point[], width: number, height: number, pad = 4): ChartGeometry | null {
  if (points.length === 0) return null;
  const times = points.map((p) => Date.parse(p.time));
  const t0 = Math.min(...times);
  const t1 = Math.max(...times);
  const low = Math.min(...points.map((p) => p.min));
  const high = Math.max(...points.map((p) => p.max));
  const span = high - low || 1;
  const x = (t: number) => pad + (t1 === t0 ? (width - 2 * pad) / 2 : ((t - t0) / (t1 - t0)) * (width - 2 * pad));
  const y = (v: number) => height - pad - ((v - low) / span) * (height - 2 * pad);
  const fmt = (n: number) => n.toFixed(1);
  const line = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${fmt(x(times[i] ?? t0))} ${fmt(y(p.avg))}`)
    .join(" ");
  const upper = points.map((p, i) => `${fmt(x(times[i] ?? t0))} ${fmt(y(p.max))}`);
  const lower = points.map((p, i) => `${fmt(x(times[i] ?? t0))} ${fmt(y(p.min))}`).reverse();
  return { line, band: `M${[...upper, ...lower].join(" L")} Z`, low, high };
}
