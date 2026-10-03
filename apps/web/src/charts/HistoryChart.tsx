import { geometry, type Point } from "./scale";

const WIDTH = 480;
const HEIGHT = 140;

export function HistoryChart({ points, unit, label }: { points: Point[]; unit: string; label: string }) {
  const shape = geometry(points, WIDTH, HEIGHT);
  if (!shape) return <p className="muted">No readings in this period.</p>;
  const first = points[0];
  const last = points.at(-1);
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${String(WIDTH)} ${String(HEIGHT)}`} width="100%" role="img" aria-label={label}>
        <path d={shape.band} className="band" />
        <path d={shape.line} className="line" />
      </svg>
      <figcaption>
        <span>
          {shape.low.toFixed(1)}–{shape.high.toFixed(1)} {unit}
        </span>
        {first && last && (
          <span className="muted">
            {new Date(first.time).toLocaleString()} → {new Date(last.time).toLocaleString()}
          </span>
        )}
      </figcaption>
    </figure>
  );
}
