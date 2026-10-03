"""The delivery queue's policy: retries with backoff, giving up, and skipping what no
longer needs sending."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from smarthome.modules.notifications.application.dispatch import DeliveryDispatcher
from smarthome.modules.notifications.application.ports import DeliveryError
from smarthome.modules.notifications.domain.model import (
    AlertStatus,
    Channel,
    Delivery,
    DeliveryStatus,
    Timing,
    Webhook,
)

HOME = UUID("00000000-0000-0000-0000-00000000000a")
T0 = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)


@dataclass
class Settled:
    status: DeliveryStatus
    attempts: int
    error: str | None
    next_attempt_at: datetime | None


@dataclass
class Store:
    due: list[Delivery] = field(default_factory=list)
    settled: dict[int, Settled] = field(default_factory=dict)
    alert_status: dict[UUID, AlertStatus] = field(default_factory=dict)
    sent_recently: int = 0
    webhook: Webhook | None = None


class FakeUnitOfWork:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.deliveries = self
        self.alerts = self
        self.webhooks = self
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def claim_due(self, now: datetime, *, limit: int) -> list[Delivery]:
        return [d for d in self.store.due if d.next_attempt_at <= now][:limit]

    async def settle(
        self,
        delivery_id: int,
        status: DeliveryStatus,
        *,
        attempts: int,
        at: datetime,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        self.store.settled[delivery_id] = Settled(status, attempts, error, next_attempt_at)

    async def sent_since(self, channel: Channel, target: str, since: datetime) -> int:
        return self.store.sent_recently

    async def overdue(self, now: datetime) -> int:
        return len([d for d in self.store.due if d.id not in self.store.settled])

    async def status_of(self, alert_id: UUID) -> AlertStatus | None:
        return self.store.alert_status.get(alert_id)

    async def get(self, home_id: UUID) -> Webhook | None:
        return self.store.webhook


class Email:
    def __init__(self, error: DeliveryError | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to: str, subject: str, body: str) -> None:
        if self.error:
            raise self.error
        self.sent.append({"to": to, "subject": subject, "body": body})


class Hook:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, *, url: str, secret: str, body: bytes, idempotency_key: str) -> None:
        self.posts.append({"url": url, "secret": secret, "body": body, "key": idempotency_key})


class Clock:
    def now(self) -> datetime:
        return T0


def email(attempts: int = 0, alert_id: UUID | None = None) -> Delivery:
    return Delivery(
        id=1,
        home_id=HOME,
        alert_id=alert_id or uuid4(),
        channel=Channel.EMAIL,
        target="alice@example.com",
        payload={"subject": "[warning] Hall sensor is offline", "body": "..."},
        idempotency_key="k1",
        attempts=attempts,
        next_attempt_at=T0,
        user_id="alice",
    )


def dispatcher(
    store: Store, mail: Email | None = None, hook: Hook | None = None
) -> DeliveryDispatcher:
    return DeliveryDispatcher(
        lambda: FakeUnitOfWork(store),  # type: ignore[arg-type]
        email=mail or Email(),
        webhook=hook or Hook(),
        clock=Clock(),
        timing=Timing(),
    )


async def test_a_due_email_is_sent_and_settled() -> None:
    store, mail = Store(due=[email()]), Email()
    assert await dispatcher(store, mail).run_once() == 1
    assert mail.sent[0]["to"] == "alice@example.com"
    assert store.settled[1] == Settled(DeliveryStatus.SENT, 1, None, None)


async def test_a_transient_failure_is_retried_later_with_backoff() -> None:
    store = Store(due=[email(attempts=2)])
    await dispatcher(store, Email(DeliveryError("smtp: connection refused"))).run_once()
    settled = store.settled[1]
    assert settled.status is DeliveryStatus.QUEUED
    assert settled.attempts == 3
    assert settled.next_attempt_at == T0 + timedelta(minutes=2)
    assert settled.error == "smtp: connection refused"


@pytest.mark.parametrize(
    ("attempts", "permanent"), [(7, False), (0, True)], ids=["out of attempts", "permanent"]
)
async def test_a_delivery_is_given_up_when_permanent_or_out_of_attempts(
    attempts: int, permanent: bool
) -> None:
    store = Store(due=[email(attempts=attempts)])
    await dispatcher(store, Email(DeliveryError("nope", permanent=permanent))).run_once()
    assert store.settled[1].status is DeliveryStatus.FAILED
    assert store.settled[1].next_attempt_at is None


async def test_an_email_held_by_quiet_hours_is_skipped_if_the_alert_resolved_meanwhile() -> None:
    alert = uuid4()
    store, mail = (
        Store(due=[email(alert_id=alert)], alert_status={alert: AlertStatus.RESOLVED}),
        Email(),
    )
    await dispatcher(store, mail).run_once()
    assert mail.sent == []
    assert store.settled[1].status is DeliveryStatus.SKIPPED
    assert store.settled[1].error == "alert resolved before delivery"


async def test_emails_over_the_hourly_limit_are_skipped() -> None:
    store, mail = Store(due=[email()], sent_recently=10), Email()
    await dispatcher(store, mail).run_once()
    assert mail.sent == []
    assert store.settled[1].error == "rate limited"


async def test_webhooks_use_the_current_secret_and_are_skipped_once_disabled() -> None:
    delivery = Delivery(
        id=1,
        home_id=HOME,
        alert_id=None,
        channel=Channel.WEBHOOK,
        target="https://old.example.com/hook",
        payload={"type": "ping"},
        idempotency_key="ping:1",
        attempts=0,
        next_attempt_at=T0,
    )
    rotated = Webhook(HOME, "https://new.example.com/hook", "n" * 43, True, "alice", T0)
    store, hook = Store(due=[delivery], webhook=rotated), Hook()
    await dispatcher(store, hook=hook).run_once()
    assert hook.posts == [
        {"url": rotated.url, "secret": rotated.secret, "body": b'{"type":"ping"}', "key": "ping:1"}
    ]

    disabled = Store(due=[delivery], webhook=Webhook(HOME, rotated.url, "n" * 43, False, "a", T0))
    hook = Hook()
    await dispatcher(disabled, hook=hook).run_once()
    assert hook.posts == []
    assert disabled.settled[1].status is DeliveryStatus.SKIPPED
