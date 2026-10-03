"""Alerts (conditions in a home that someone should know about) and the notifications
they produce.

An alert has a key (`device_offline:<device>`, `low_battery:<device>`,
`energy_budget:<yyyy-mm>:<percent>`) and at most one live instance per key and home:
`pending` (waiting out a grace period), then `open`, then `resolved`. People are told when
an alert opens and when it resolves, never again while it stays open, so a flapping
sensor cannot flood anyone's inbox.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any
from uuid import UUID


class AlertKind(StrEnum):
    DEVICE_OFFLINE = "device_offline"
    LOW_BATTERY = "low_battery"
    ENERGY_BUDGET = "energy_budget"


class Severity(IntEnum):
    INFO = 1
    WARNING = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, label: str) -> "Severity":
        return cls[label.upper()]


class AlertStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class Alert:
    id: UUID
    home_id: UUID
    key: str
    kind: AlertKind
    severity: Severity
    device_id: str | None
    title: str
    details: dict[str, Any]
    status: AlertStatus
    observed_at: datetime  # time of the observation behind the last transition
    created_at: datetime
    due_at: datetime | None = None  # pending only: when it opens if nothing clears it
    opened_at: datetime | None = None
    resolved_at: datetime | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None


class NotificationEvent(StrEnum):
    OPENED = "opened"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class Notification:
    """One entry in one person's inbox."""

    id: UUID
    user_id: str
    home_id: UUID
    alert_id: UUID | None
    event: NotificationEvent
    severity: Severity
    title: str
    body: str
    created_at: datetime
    read_at: datetime | None = None


class Channel(StrEnum):
    EMAIL = "email"
    WEBHOOK = "webhook"


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"  # gave up: permanent error or out of attempts
    SKIPPED = "skipped"  # deliberately not sent (rate limit, alert resolved meanwhile)


@dataclass(frozen=True, slots=True)
class Delivery:
    id: int
    home_id: UUID
    alert_id: UUID | None
    channel: Channel
    target: str  # an email address, or the home's webhook URL at enqueue time
    payload: dict[str, Any]
    idempotency_key: str
    attempts: int
    next_attempt_at: datetime
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class Preferences:
    """One member's choices for one home. Quiet hours are whole hours of home time;
    start > end wraps past midnight (22 -> 7)."""

    email_enabled: bool = True
    min_email_severity: Severity = Severity.WARNING
    quiet_start: int | None = None
    quiet_end: int | None = None

    def __post_init__(self) -> None:
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("quiet hours need both a start and an end")
        for hour in (self.quiet_start, self.quiet_end):
            if hour is not None and not 0 <= hour <= 23:  # noqa: PLR2004
                raise ValueError("quiet hours are 0 to 23")
        if self.quiet_start is not None and self.quiet_start == self.quiet_end:
            raise ValueError("quiet hours must not start and end at the same hour")


@dataclass(frozen=True, slots=True)
class Webhook:
    home_id: UUID
    url: str
    secret: str
    enabled: bool
    updated_by: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Timing:
    offline_grace: timedelta = timedelta(minutes=5)
    battery_low_pct: float = 15.0
    battery_ok_pct: float = 20.0  # hysteresis: a reading between the two changes nothing
    budget_thresholds: tuple[int, ...] = (80, 100)
    max_attempts: int = 8
    first_retry: timedelta = timedelta(seconds=30)
    max_retry: timedelta = timedelta(hours=1)
    emails_per_hour: int = 10

    def retry_after(self, attempts: int) -> timedelta:
        """Exponential backoff after the given number of failed attempts."""
        delay: timedelta = self.first_retry * 2 ** max(0, attempts - 1)
        return min(self.max_retry, delay)


@dataclass(frozen=True, slots=True)
class Raise:
    """Make sure an alert with this key is live (pending until `due_at`, or open)."""

    key: str
    kind: AlertKind
    severity: Severity
    device_id: str | None
    title: str
    observed_at: datetime
    due_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Clear:
    """Resolve the live alert with this key, if any (a pending one is just dropped)."""

    key: str
    observed_at: datetime
