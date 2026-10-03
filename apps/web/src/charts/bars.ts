// Bars for a series of totals (energy per hour or day): one rectangle per bucket,
// scaled to the largest bucket, with a little gap between bars.

export interface Bar {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function bars(values: number[], width: number, height: number, gap = 2): Bar[] {
  if (values.length === 0) return [];
  const top = Math.max(...values, 0) || 1;
  const slot = width / values.length;
  const barWidth = Math.max(1, slot - gap);
  return values.map((value, i) => {
    const h = (Math.max(0, value) / top) * height;
    return { x: i * slot + (slot - barWidth) / 2, y: height - h, width: barWidth, height: h };
  });
}
