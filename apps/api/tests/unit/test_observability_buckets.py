"""Duration histograms get seconds-scale buckets (the SDK defaults assume milliseconds)."""

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

from smarthome.shared.observability import SECONDS_HISTOGRAMS


def test_a_seconds_histogram_separates_milliseconds_from_seconds() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader], views=[SECONDS_HISTOGRAMS]).get_meter("t")
    durations = meter.create_histogram("t.duration", unit="s")
    count = meter.create_histogram("t.batch", unit="{row}")
    for value in (0.003, 0.04, 0.3, 4.0):
        durations.record(value)
        count.record(value)

    data = reader.get_metrics_data()
    assert data is not None
    points = {
        m.name: m.data.data_points[0]
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    seconds = points["t.duration"]
    assert isinstance(seconds, HistogramDataPoint)
    # Four samples, four different buckets.
    assert sum(1 for c in seconds.bucket_counts if c) == 4
    # Other units keep the defaults.
    assert points["t.batch"].explicit_bounds[1] == 5.0  # type: ignore[union-attr]
