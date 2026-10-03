from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from smarthome.modules.notifications.domain.model import (
    Alert,
    AlertKind,
    AlertStatus,
    Channel,
    Delivery,
    DeliveryStatus,
    Notification,
    Preferences,
    Webhook,
)
from smarthome.modules.notifications.domain.rules import DeviceInfo
from smarthome.shared.audit import AuditEvent


@dataclass(frozen=True, slots=True)
class NewDelivery:
    home_id: UUID
    alert_id: UUID | None
    user_id: str | None
    channel: Channel
    target: str
    payload: dict[str, Any]
    idempotency_key: str
    next_attempt_at: datetime


class AlertStore(Protocol):
    async def insert(self, alert: Alert) -> bool:
        """False if an alert with the same key is already live in the home."""
        ...

    async def last_observed(self, home_id: UUID, key: str) -> datetime | None:
        """Observation time of the newest transition of any alert with this key."""
        ...

    async def touch(self, home_id: UUID, key: str, observed_at: datetime) -> None:
        """Record a newer observation on the live alert (a raise that changed nothing)."""
        ...

    async def clear(
        self, home_id: UUID, key: str, *, at: datetime, observed_at: datetime
    ) -> Alert | None:
        """Drop a pending alert or resolve an open one. Returns it as it was before."""
        ...

    async def claim_due(self, now: datetime, *, limit: int) -> list[Alert]:
        """Pending alerts past their grace period, locked; others skip them."""
        ...

    async def mark_open(self, alert_id: UUID, *, at: datetime) -> Alert: ...
    async def get(self, home_id: UUID, alert_id: UUID) -> Alert | None: ...
    async def status_of(self, alert_id: UUID) -> AlertStatus | None: ...
    async def for_home(
        self, home_id: UUID, *, statuses: frozenset[AlertStatus], limit: int
    ) -> list[Alert]: ...
    async def live_of_kind(self, home_id: UUID, kind: AlertKind) -> list[Alert]: ...
    async def acknowledge(self, alert_id: UUID, *, by: str, at: datetime) -> Alert: ...
    async def purge(self, *, resolved_before: datetime) -> int: ...


class Inbox(Protocol):
    async def add(self, notifications: list[Notification]) -> None: ...
    async def for_user(
        self,
        user_id: str,
        homes: list[UUID],
        *,
        unread_only: bool,
        limit: int,
        before: datetime | None,
    ) -> list[Notification]: ...
    async def unread_count(self, user_id: str, homes: list[UUID]) -> int: ...
    async def mark_read(self, user_id: str, notification_id: UUID, *, at: datetime) -> bool: ...
    async def mark_all_read(self, user_id: str, homes: list[UUID], *, at: datetime) -> int: ...
    async def purge(self, *, created_before: datetime) -> int: ...


class Deliveries(Protocol):
    async def enqueue(self, deliveries: list[NewDelivery]) -> None:
        """Idempotent per idempotency key."""
        ...

    async def claim_due(self, now: datetime, *, limit: int) -> list[Delivery]: ...
    async def settle(
        self,
        delivery_id: int,
        status: DeliveryStatus,
        *,
        attempts: int,
        at: datetime,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None: ...
    async def sent_since(self, channel: Channel, target: str, since: datetime) -> int: ...
    async def purge(self, *, settled_before: datetime) -> int: ...


class PreferenceStore(Protocol):
    async def for_home(self, home_id: UUID) -> dict[str, Preferences]: ...
    async def save(
        self, home_id: UUID, user_id: str, prefs: Preferences, *, at: datetime
    ) -> None: ...


class Webhooks(Protocol):
    async def get(self, home_id: UUID) -> Webhook | None: ...
    async def save(self, webhook: Webhook) -> None: ...
    async def delete(self, home_id: UUID) -> bool: ...


class AuditLog(Protocol):
    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None: ...


class NotificationsUnitOfWork(Protocol):
    @property
    def alerts(self) -> AlertStore: ...
    @property
    def inbox(self) -> Inbox: ...
    @property
    def deliveries(self) -> Deliveries: ...
    @property
    def preferences(self) -> PreferenceStore: ...
    @property
    def webhooks(self) -> Webhooks: ...
    @property
    def audit(self) -> AuditLog: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...


class NotificationsUnitOfWorkFactory(Protocol):
    def __call__(self) -> NotificationsUnitOfWork: ...


# --- Other modules and the outside world ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: str
    email: str | None


@dataclass(frozen=True, slots=True)
class Audience:
    timezone: str
    recipients: tuple[Recipient, ...]


class Homes(Protocol):
    async def audience(self, home_id: UUID) -> Audience | None: ...


class Devices(Protocol):
    async def info(self, home_id: UUID, device_id: str) -> DeviceInfo | None: ...
    async def is_online(self, home_id: UUID, device_id: str) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class Budget:
    home_id: UUID
    month_start: datetime
    used_kwh: float
    budget_kwh: float


class Budgets(Protocol):
    async def budgets(self) -> list[Budget]: ...


class DeliveryError(Exception):
    """A send failed. Permanent failures (a 4xx, a refused address) are not retried."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class EmailSender(Protocol):
    async def send(self, *, to: str, subject: str, body: str) -> None:
        """Raises DeliveryError."""
        ...


class WebhookSender(Protocol):
    async def post(self, *, url: str, secret: str, body: bytes, idempotency_key: str) -> None:
        """Raises DeliveryError."""
        ...


class UrlGuard(Protocol):
    async def check(self, url: str) -> None:
        """Raises InvalidWebhook if the URL points somewhere a hub must not call (private
        or loopback addresses outside development)."""
        ...
