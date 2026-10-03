---
status: accepted
date: 2026-10-03
---

# 0012. Energy accounting: hourly rollup of counters, exact tariffs, meter over plugs

## Context and problem statement

Smart plugs and the whole-home energy meter report `energy_wh_total`, a cumulative
counter (and `power_w`, an instantaneous reading). People want to know how much they
used per hour or per day, on what, and what it cost, including on time-of-use tariffs
such as Brazil's *tarifa branca* (a different price on weekday evenings).

Questions to settle:

- Where does "kWh per hour per device" come from, and how does it survive reboots
  (counters restart from zero), late or duplicated telemetry, and raw-data retention
  (30 days, ADR 0007)?
- How are prices stored and costs computed without float rounding errors?
- The meter already includes every plug. What is the home's total?

## Decision drivers

- Correct under redelivery and late data: the same readings always give the same totals.
- Reports for a year must be cheap and must outlive raw telemetry.
- Money is exact.
- Several worker replicas run side by side (ADR 0001).

## Considered options

For consumption:

1. Integrate `power_w` over time.
2. Compute deltas of `energy_wh_total` on every request, from raw readings or the
   continuous aggregates.
3. **A worker rollup of counter deltas into `energy.hourly`**, recomputing recent hours.

For prices: float; integer minor units per kWh; **`numeric` and `Decimal`**.

## Decision outcome

### Counter deltas, rolled up per hour

`telemetry.public` gains `hourly_increase(metric, start, end)`: for each device, the
difference between consecutive samples (`lag()` over time), summed per hour.

- **Resets.** A sample lower than the one before means the device restarted its counter
  from zero, so the increase is the new value itself, never a negative delta.
- **Window edges.** The query starts 6 hours before the window so the first sample inside
  it has a predecessor. An increase that spans a gap is attributed to the hour of the
  later sample; a gap longer than 6 hours loses that delta (the device was off the
  network that long; documented, not hidden).

The worker's **energy rollup** runs every minute. It takes a transaction-scoped advisory
lock (`pg_try_advisory_xact_lock`): a second worker skips the run instead of queueing.
It recomputes from two hours before its cursor up to now, in day-sized chunks, and
**replaces** the rows of those hours (`INSERT … ON CONFLICT DO UPDATE … WHERE wh IS
DISTINCT FROM`). Recomputation, not accumulation, is what makes it idempotent: a run that
crashes before commit, or that sees a late or redelivered reading, simply produces the
right numbers next time. On first start it backfills a week.

`energy.hourly` (device, hour, home, Wh) is about 9k rows per device per year, kept
indefinitely; reports read it and never touch raw telemetry.

### Exact prices

A home has one tariff: an ISO 4217 currency, a base price per kWh, optional time-of-use
periods (weekdays + whole local hours, no wrap past midnight, no overlaps) and an
optional monthly budget in kWh. Prices are `numeric(12,6)` in Postgres and `Decimal` in
Python; the API accepts them only as decimal strings (a JSON float has already lost the
value the user typed). Each hour is priced at the rate that applies at its local start;
costs are summed exactly and rounded **once**, half-to-even, to the currency's minor
unit. Periods are whole hours because consumption is accounted per hour: a period from
17:30 could not be priced honestly.

Tariff edits are versioned like automations: `ETag`/`If-Match`, `428` without it once a
tariff exists, `412` on a stale version. The first save is serialised by an advisory
lock on the home (two "create" requests would otherwise both see no row); a test that
holds the read open shows two concurrent first saves both winning without it.

### The home's total

When a whole-home meter reported in the range, the total **is** the meter, plugs are a
breakdown, and `unmetered` = meter − plugs (lights, the shower, everything without a
plug). Without a meter, the total is the sum of the plugs and the report says so
(`measured_by: submeters`).

### Consequences

- Good, because totals are reproducible from raw readings and stay correct under
  redelivery, reboots and late data.
- Good, because a year of daily buckets reads a few thousand rows.
- Good, because money never touches a float.
- Bad, because consumption appears with up to a minute of delay (the rollup interval),
  and the current hour is partial until it ends.
- Bad, because a device offline for more than 6 hours loses the energy it used while
  away (its counter jump is not attributed to any hour). Raising the lookback is a
  one-line change if real devices need it.
- Bad, because the choice between meter and plugs is per report, not per hour: a meter
  that was offline for part of the range undercounts that part.

### Confirmation

- Unit tests: tariff validation and pricing per hour, half-even rounding per currency,
  meter vs plugs, daily buckets at local midnight, budget month boundaries.
- Integration tests on TimescaleDB: counter reset and lookback, rollup idempotency and
  late readings, a held lock makes a second rollup skip, concurrent first tariff saves.

## Pros and cons of the options

### Integrate `power_w`

- Good, because every powered device reports it, lights included.
- Bad, because the result depends on the sampling rate and drifts from what the utility
  bills; counters are what meters are for.

### Deltas on request

- Good, because nothing new to run.
- Bad, because a year-long report scans raw data that has already expired, and the
  continuous aggregates' `last` per bucket cannot see a reset inside a bucket.
