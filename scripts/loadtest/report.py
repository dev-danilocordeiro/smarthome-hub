"""Summarise a load test window from Prometheus as a Markdown table.

    python3 scripts/loadtest/report.py --start 1759500000 --end 1759500300

Every figure is computed over [start, end] with `increase()`/`rate()` evaluated at
`end`, so it describes the test and nothing before it. Stdlib only.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

PROMETHEUS = "http://localhost:9090"


def instant(query: str, at: float) -> float | None:
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": at})
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - local Prometheus
        result = json.load(response)["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def quantile(metric: str, q: float, window: str, where: str = "") -> str:
    selector = f"{metric}_bucket{{{where}}}" if where else f"{metric}_bucket"
    return f"histogram_quantile({q}, sum by (le) (rate({selector}[{window}])))"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    args = parser.parse_args()
    seconds = int(args.end - args.start)
    w = f"{seconds}s"

    accepted = 'smarthome_ingestor_messages_total{outcome="accepted"}'
    others = 'smarthome_ingestor_messages_total{outcome!="accepted"}'
    written = 'smarthome_telemetry_rows_total{outcome="written"}'
    batch = "smarthome_telemetry_batch_size"
    rows: list[tuple[str, str, float | None, str]] = [
        (
            "Device messages accepted",
            "msg/s",
            instant(
                f"sum(increase({accepted}[{w}])) / {seconds}",
                args.end,
            ),
            "%.0f",
        ),
        (
            "Messages not accepted (ignored, rejected)",
            "msg/s",
            instant(
                f"sum(increase({others}[{w}])) / {seconds}",
                args.end,
            ),
            "%.1f",
        ),
        (
            "Rows written",
            "rows/s",
            instant(
                f"sum(increase({written}[{w}])) / {seconds}",
                args.end,
            ),
            "%.0f",
        ),
        (
            "Ingest latency p50 (device ts to committed row)",
            "ms",
            _ms(instant(quantile("smarthome_telemetry_ingest_latency_seconds", 0.5, w), args.end)),
            "%.0f",
        ),
        (
            "Ingest latency p95",
            "ms",
            _ms(instant(quantile("smarthome_telemetry_ingest_latency_seconds", 0.95, w), args.end)),
            "%.0f",
        ),
        (
            "Ingest latency p99",
            "ms",
            _ms(instant(quantile("smarthome_telemetry_ingest_latency_seconds", 0.99, w), args.end)),
            "%.0f",
        ),
        (
            "Handler time p95 (per MQTT message)",
            "ms",
            _ms(
                instant(quantile("smarthome_ingestor_handling_duration_seconds", 0.95, w), args.end)
            ),
            "%.1f",
        ),
        (
            "Flush duration p95 (one batch INSERT)",
            "ms",
            _ms(instant(quantile("smarthome_telemetry_flush_duration_seconds", 0.95, w), args.end)),
            "%.0f",
        ),
        (
            "Average batch",
            "rows",
            instant(
                f"sum(increase({batch}_sum[{w}])) / sum(increase({batch}_count[{w}]))",
                args.end,
            ),
            "%.0f",
        ),
        (
            "Write buffer, max",
            "rows",
            instant(f"max(max_over_time(smarthome_telemetry_buffer_size[{w}]))", args.end),
            "%.0f",
        ),
        (
            "Device event stream lag p95 (automations)",
            "ms",
            _ms(
                instant(
                    quantile("smarthome_events_lag_seconds", 0.95, w, 'group="automations"'),
                    args.end,
                )
            ),
            "%.0f",
        ),
        (
            "Device event stream lag p95 (notifications)",
            "ms",
            _ms(
                instant(
                    quantile("smarthome_events_lag_seconds", 0.95, w, 'group="notifications"'),
                    args.end,
                )
            ),
            "%.0f",
        ),
        (
            "Ingestor CPU, average",
            "% of one core",
            _pct(
                instant(
                    f'avg(avg_over_time(process_cpu_utilization_ratio{{service_name="smarthome-ingestor"}}[{w}]))',
                    args.end,
                )
            ),
            "%.0f",
        ),
        (
            "Ingestor memory, max",
            "MiB",
            _mib(
                instant(
                    f'max(max_over_time(process_memory_usage_bytes{{service_name="smarthome-ingestor"}}[{w}]))',
                    args.end,
                )
            ),
            "%.0f",
        ),
    ]
    print(f"| Measure ({seconds} s window) | Value | Unit |")
    print("|---|---:|---|")
    for label, unit, value, fmt in rows:
        print(f"| {label} | {fmt % value if value is not None else 'n/a'} | {unit} |")
    return 0


def _ms(seconds: float | None) -> float | None:
    return seconds * 1000 if seconds is not None else None


def _pct(ratio: float | None) -> float | None:
    # process.cpu.utilization is normalised over every CPU of the host; report one core.
    return ratio * 100 * (os.cpu_count() or 1) if ratio is not None else None


def _mib(value: float | None) -> float | None:
    return value / 2**20 if value is not None else None


if __name__ == "__main__":
    sys.exit(main())
