import { useState } from "react";
import { api, ApiProblem, describe, type Tariff, type TariffIn, unwrap, type Usage } from "../api/client";
import { bars } from "../charts/bars";
import { useResource } from "../hooks/useResource";

const WIDTH = 640;
const HEIGHT = 160;
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

type RangeKey = "24h" | "7d" | "month" | "30d";

export function range(key: RangeKey, now = new Date()): { from: Date; to: Date; bucket: "hour" | "day" } {
  const to = now;
  switch (key) {
    case "24h":
      return { from: new Date(now.getTime() - 24 * 3600_000), to, bucket: "hour" };
    case "7d":
      return { from: startOfDay(new Date(now.getTime() - 6 * 86_400_000)), to, bucket: "day" };
    case "month":
      return { from: new Date(now.getFullYear(), now.getMonth(), 1), to, bucket: "day" };
    case "30d":
      return { from: startOfDay(new Date(now.getTime() - 29 * 86_400_000)), to, bucket: "day" };
  }
}

function startOfDay(at: Date): Date {
  return new Date(at.getFullYear(), at.getMonth(), at.getDate());
}

export function kwh(wh: number): string {
  return (wh / 1000).toLocaleString(undefined, { maximumFractionDigits: wh < 10_000 ? 2 : 1 });
}

function money(amount: string | null, currency: string | null): string {
  if (amount === null || currency === null) return "";
  return Number(amount).toLocaleString(undefined, { style: "currency", currency });
}

export function EnergyPage({ homeId, canManage }: { homeId: string; canManage: boolean }) {
  const [rangeKey, setRangeKey] = useState<RangeKey>("7d");
  const { from, to, bucket } = range(rangeKey);
  const { data: usage, error } = useResource(
    () =>
      unwrap(
        api.GET("/homes/{home_id}/energy/usage", {
          params: {
            path: { home_id: homeId },
            query: { from: from.toISOString(), to: to.toISOString(), bucket },
          },
        }),
      ),
    `${homeId}:${rangeKey}`,
  );

  return (
    <div className="energy">
      <div className="row">
        <h2 className="grow">Energy</h2>
        <label>
          Period{" "}
          <select value={rangeKey} onChange={(e) => { setRangeKey(e.target.value as RangeKey); }}>
            <option value="24h">Last 24 hours</option>
            <option value="7d">Last 7 days</option>
            <option value="month">This month</option>
            <option value="30d">Last 30 days</option>
          </select>
        </label>
      </div>
      {error !== null && <p role="alert">{describe(error)}</p>}
      {usage === null ? <p>Loading…</p> : <UsageView usage={usage} />}
      <TariffEditor homeId={homeId} canManage={canManage} />
    </div>
  );
}

function UsageView({ usage }: { usage: Usage }) {
  const shapes = bars(
    usage.buckets.map((b) => b.wh),
    WIDTH,
    HEIGHT,
  );
  const label = (start: string) =>
    usage.bucket === "hour"
      ? new Date(start).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
      : new Date(start).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const top = Math.max(...usage.devices.map((d) => d.wh), 1);

  return (
    <>
      <p className="totals">
        <strong>{kwh(usage.total_wh)} kWh</strong>
        {usage.total_cost !== null && <> · {money(usage.total_cost, usage.currency)}</>}
        <span className="muted">
          {" "}
          · {usage.measured_by === "meter" ? "whole-home meter" : "sum of smart plugs (no meter)"}
        </span>
      </p>
      {usage.total_wh === 0 ? (
        <p className="muted">No consumption recorded in this period yet.</p>
      ) : (
        <figure className="chart">
          <svg
            viewBox={`0 0 ${String(WIDTH)} ${String(HEIGHT)}`}
            width="100%"
            role="img"
            aria-label={`Consumption per ${usage.bucket}`}
          >
            {shapes.map((shape, i) => {
              const b = usage.buckets[i];
              return (
                b && (
                  <rect key={b.start} className="bar" x={shape.x} y={shape.y} width={shape.width} height={shape.height}>
                    <title>
                      {label(b.start)}: {kwh(b.wh)} kWh{b.cost !== null ? ` · ${money(b.cost, usage.currency)}` : ""}
                    </title>
                  </rect>
                )
              );
            })}
          </svg>
          <figcaption>
            <span className="muted">{usage.buckets[0] && label(usage.buckets[0].start)}</span>
            <span className="muted">{usage.buckets.at(-1) && label(usage.buckets.at(-1)?.start ?? "")}</span>
          </figcaption>
        </figure>
      )}
      {usage.devices.length > 0 && (
        <table className="breakdown">
          <thead>
            <tr>
              <th scope="col">Device</th>
              <th scope="col" className="num">kWh</th>
              {usage.currency && <th scope="col" className="num">Cost</th>}
              <th scope="col" aria-label="Share" />
            </tr>
          </thead>
          <tbody>
            {usage.devices.map((d) => (
              <tr key={d.device_id}>
                <td>
                  {d.name ?? d.device_id}
                  {d.whole_home && <span className="badge">meter</span>}
                  {d.room && <span className="muted"> · {d.room}</span>}
                </td>
                <td className="num">{kwh(d.wh)}</td>
                {usage.currency && <td className="num">{money(d.cost, usage.currency)}</td>}
                <td className="share">
                  <span style={{ width: `${String(Math.round((d.wh / top) * 100))}%` }} />
                </td>
              </tr>
            ))}
            {usage.unmetered_wh !== null && (
              <tr className="muted">
                <td>Everything without a plug</td>
                <td className="num">{kwh(usage.unmetered_wh)}</td>
                {usage.currency && <td />}
                <td />
              </tr>
            )}
          </tbody>
        </table>
      )}
    </>
  );
}

type Period = NonNullable<TariffIn["periods"]>[number];

interface Draft extends TariffIn {
  version: number | null;
}

const EMPTY_DRAFT: Draft = { currency: "BRL", base_price: "", periods: [], monthly_budget_kwh: null, version: null };

async function loadTariff(homeId: string): Promise<Draft> {
  const { data, response } = await api.GET("/homes/{home_id}/energy/tariff", {
    params: { path: { home_id: homeId } },
  });
  if (response.status === 404 || !data) return EMPTY_DRAFT;
  return draftOf(data);
}

function draftOf(tariff: Tariff): Draft {
  return {
    currency: tariff.currency,
    base_price: tariff.base_price,
    periods: tariff.periods,
    monthly_budget_kwh: tariff.monthly_budget_kwh,
    version: tariff.version,
  };
}

function TariffEditor({ homeId, canManage }: { homeId: string; canManage: boolean }) {
  const { data: loaded, error } = useResource(() => loadTariff(homeId), homeId);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const current = draft ?? loaded;
  if (error !== null) return <p role="alert">{describe(error)}</p>;
  if (current === null) return null;

  const update = (change: Partial<Draft>) => { setDraft({ ...current, ...change }); };
  const updatePeriod = (index: number, change: Partial<Period>) => {
    update({ periods: (current.periods ?? []).map((p, i) => (i === index ? { ...p, ...change } : p)) });
  };

  async function save() {
    if (current === null) return;
    setMessage(null);
    const { version, ...body } = current;
    try {
      const saved = await unwrap(
        api.PUT("/homes/{home_id}/energy/tariff", {
          params: {
            path: { home_id: homeId },
            header: version === null ? {} : { "if-match": `"${String(version)}"` },
          },
          body: { ...body, monthly_budget_kwh: body.monthly_budget_kwh || null },
        }),
      );
      setDraft(draftOf(saved));
      setMessage("Saved.");
    } catch (failure) {
      setMessage(
        failure instanceof ApiProblem && failure.status === 412
          ? "Someone else changed the tariff; reload to see it."
          : describe(failure),
      );
    }
  }

  return (
    <form
      className="capture tariff"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h3>Tariff and budget</h3>
      {current.version === null && <p className="muted">No tariff yet: consumption is shown without cost.</p>}
      <fieldset disabled={!canManage}>
        <div className="row">
          <label>
            Currency{" "}
            <input
              value={current.currency}
              onChange={(e) => { update({ currency: e.target.value.toUpperCase() }); }}
              pattern="[A-Z]{3}"
              size={4}
              required
            />
          </label>
          <label>
            Price per kWh{" "}
            <input
              value={current.base_price}
              onChange={(e) => { update({ base_price: e.target.value }); }}
              inputMode="decimal"
              pattern="\d{1,4}(\.\d{1,6})?"
              placeholder="0.891234"
              required
            />
          </label>
          <label>
            Monthly budget (kWh){" "}
            <input
              value={current.monthly_budget_kwh ?? ""}
              onChange={(e) => { update({ monthly_budget_kwh: e.target.value || null }); }}
              inputMode="decimal"
              pattern="\d{1,7}(\.\d{1,3})?"
              size={8}
            />
          </label>
        </div>
        {(current.periods ?? []).map((period, index) => (
          <div className="row period" key={index}>
            <input
              aria-label="Period name"
              value={period.name}
              onChange={(e) => { updatePeriod(index, { name: e.target.value }); }}
              size={10}
              required
            />
            {WEEKDAYS.map((day, weekday) => (
              <label key={day} className="day">
                <input
                  type="checkbox"
                  checked={period.weekdays.includes(weekday)}
                  onChange={(e) => {
                    updatePeriod(index, {
                      weekdays: e.target.checked
                        ? [...period.weekdays, weekday].sort()
                        : period.weekdays.filter((d) => d !== weekday),
                    });
                  }}
                />
                {day}
              </label>
            ))}
            <HourSelect label="From" value={period.start} onChange={(start) => { updatePeriod(index, { start }); }} />
            <HourSelect label="To" value={period.end} end onChange={(end) => { updatePeriod(index, { end }); }} />
            <input
              aria-label="Period price per kWh"
              value={period.price}
              onChange={(e) => { updatePeriod(index, { price: e.target.value }); }}
              inputMode="decimal"
              size={9}
              required
            />
            <button
              type="button"
              className="ghost"
              onClick={() => { update({ periods: (current.periods ?? []).filter((_, i) => i !== index) }); }}
            >
              Remove
            </button>
          </div>
        ))}
        <div className="row">
          <button
            type="button"
            className="ghost"
            onClick={() => {
              update({
                periods: [
                  ...(current.periods ?? []),
                  { name: "Peak", weekdays: [0, 1, 2, 3, 4], start: "18:00", end: "21:00", price: current.base_price },
                ],
              });
            }}
          >
            Add time-of-use period
          </button>
          <button type="submit">Save tariff</button>
          {message && <span role="status">{message}</span>}
        </div>
      </fieldset>
    </form>
  );
}

function HourSelect({
  label,
  value,
  end = false,
  onChange,
}: {
  label: string;
  value: string;
  end?: boolean;
  onChange: (value: string) => void;
}) {
  const hours = Array.from({ length: 24 }, (_, h) => `${String(h + (end ? 1 : 0)).padStart(2, "0")}:00`);
  return (
    <label>
      {label}{" "}
      <select value={value} onChange={(e) => { onChange(e.target.value); }}>
        {hours.map((h) => (
          <option key={h} value={h}>
            {h}
          </option>
        ))}
      </select>
    </label>
  );
}
