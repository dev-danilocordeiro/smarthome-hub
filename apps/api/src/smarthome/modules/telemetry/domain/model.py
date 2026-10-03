"""Telemetry readings and how a time range maps to a storage resolution."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Reading:
    time: datetime
    home_id: UUID
    device_id: str
    metric: str
    value: float
    message_id: str
    received_at: datetime


def readings_from_message(
    *, home_id: UUID, device_id: str, payload: dict[str, Any], received_at: datetime
) -> list[Reading]:
    """One row per metric. Booleans (motion, contact_open) become 1.0 / 0.0 so every
    metric aggregates the same way: the average of `motion` is the share of time it fired."""
    time = datetime.fromisoformat(payload["ts"])
    return [
        Reading(
            time=time,
            home_id=home_id,
            device_id=device_id,
            metric=metric,
            value=float(value),
            message_id=payload["message_id"],
            received_at=received_at,
        )
        for metric, value in payload["readings"].items()
    ]


class Resolution(StrEnum):
    RAW = "raw"
    MINUTE = "1m"
    HOUR = "1h"
    DAY = "1d"


# Pick the coarsest level that still gives a useful chart: a few hundred to ~1500 points.
_AUTO = (
    (timedelta(hours=2), Resolution.RAW),
    (timedelta(days=1), Resolution.MINUTE),
    (timedelta(days=60), Resolution.HOUR),
)
MAX_RANGE = timedelta(days=3 * 365)


def choose_resolution(start: datetime, end: datetime) -> Resolution:
    span = end - start
    return next((res for limit, res in _AUTO if span <= limit), Resolution.DAY)


@dataclass(frozen=True, slots=True)
class Point:
    time: datetime
    avg: float
    min: float
    max: float
    samples: int


@dataclass(frozen=True, slots=True)
class HourlyIncrease:
    """How much a cumulative counter (`energy_wh_total`) grew within one hour."""

    home_id: UUID
    device_id: str
    hour: datetime
    increase: float


# How far before a window to look for the sample each device's first delta starts from.
COUNTER_LOOKBACK = timedelta(hours=6)
