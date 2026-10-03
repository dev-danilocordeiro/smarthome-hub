"""Delivering queued notifications (worker): email and webhooks, at least once.

Due deliveries are claimed with FOR UPDATE SKIP LOCKED, so worker replicas share the
queue without sending anything twice in normal operation. A worker that dies after a
send and before its commit leaves the row queued: it is sent again. Webhook receivers
deduplicate on the `Idempotency-Key` header; a duplicate email is the accepted cost.
"""

from datetime import timedelta

import structlog
from opentelemetry import metrics

from smarthome.modules.notifications.application.alerts import dumps
from smarthome.modules.notifications.application.ports import (
    DeliveryError,
    EmailSender,
    NotificationsUnitOfWork,
    NotificationsUnitOfWorkFactory,
    WebhookSender,
)
from smarthome.modules.notifications.domain.model import (
    AlertStatus,
    Channel,
    Delivery,
    DeliveryStatus,
    Timing,
)
from smarthome.shared.clock import Clock

log = structlog.get_logger(__name__)
meter = metrics.get_meter(__name__)

deliveries_counter = meter.create_counter(
    "smarthome.notifications.deliveries",
    unit="{delivery}",
    description="Delivery attempts by channel and outcome (sent, retry, failed, skipped).",
)
delivery_delay = meter.create_histogram(
    "smarthome.notifications.delivery.delay",
    unit="s",
    description="From the time a delivery was due to the time it was sent.",
)

BATCH = 50
RATE_WINDOW = timedelta(hours=1)


class DeliveryDispatcher:
    def __init__(
        self,
        uow: NotificationsUnitOfWorkFactory,
        *,
        email: EmailSender,
        webhook: WebhookSender,
        clock: Clock,
        timing: Timing,
    ) -> None:
        self._uow = uow
        self._email = email
        self._webhook = webhook
        self._clock = clock
        self._timing = timing

    async def run_once(self) -> int:
        """Attempt every due delivery once. Returns how many were attempted."""
        async with self._uow() as uow:
            due = await uow.deliveries.claim_due(self._clock.now(), limit=BATCH)
            for delivery in due:
                await self._attempt(uow, delivery)
            await uow.commit()
        return len(due)

    async def _attempt(self, uow: NotificationsUnitOfWork, delivery: Delivery) -> None:
        now = self._clock.now()
        attempts = delivery.attempts + 1
        skip = await self._reason_to_skip(uow, delivery)
        if skip is not None:
            await uow.deliveries.settle(
                delivery.id, DeliveryStatus.SKIPPED, attempts=delivery.attempts, at=now, error=skip
            )
            deliveries_counter.add(1, {"channel": delivery.channel.value, "outcome": "skipped"})
            return
        try:
            await self._send(uow, delivery)
        except DeliveryError as exc:
            give_up = exc.permanent or attempts >= self._timing.max_attempts
            await uow.deliveries.settle(
                delivery.id,
                DeliveryStatus.FAILED if give_up else DeliveryStatus.QUEUED,
                attempts=attempts,
                at=now,
                error=str(exc)[:500],
                next_attempt_at=None if give_up else now + self._timing.retry_after(attempts),
            )
            outcome = "failed" if give_up else "retry"
            deliveries_counter.add(1, {"channel": delivery.channel.value, "outcome": outcome})
            log.warning(
                "notification_delivery_" + outcome,
                delivery_id=delivery.id,
                channel=delivery.channel.value,
                attempts=attempts,
                error=str(exc)[:200],
            )
            return
        await uow.deliveries.settle(delivery.id, DeliveryStatus.SENT, attempts=attempts, at=now)
        deliveries_counter.add(1, {"channel": delivery.channel.value, "outcome": "sent"})
        delivery_delay.record(
            max(0.0, (now - delivery.next_attempt_at).total_seconds()),
            {"channel": delivery.channel.value},
        )

    async def _reason_to_skip(self, uow: NotificationsUnitOfWork, delivery: Delivery) -> str | None:
        if delivery.channel is Channel.EMAIL:
            # Emails announce new alerts only. Held back by quiet hours, and the alert is
            # already over: nothing left to say.
            if (
                delivery.alert_id is not None
                and await uow.alerts.status_of(delivery.alert_id) is AlertStatus.RESOLVED
            ):
                return "alert resolved before delivery"
            since = self._clock.now() - RATE_WINDOW
            sent = await uow.deliveries.sent_since(Channel.EMAIL, delivery.target, since)
            if sent >= self._timing.emails_per_hour:
                return "rate limited"
            return None
        webhook = await uow.webhooks.get(delivery.home_id)
        if webhook is None or not webhook.enabled:
            return "webhook removed or disabled"
        return None

    async def _send(self, uow: NotificationsUnitOfWork, delivery: Delivery) -> None:
        if delivery.channel is Channel.EMAIL:
            await self._email.send(
                to=delivery.target,
                subject=str(delivery.payload["subject"]),
                body=str(delivery.payload["body"]),
            )
            return
        webhook = await uow.webhooks.get(delivery.home_id)
        if webhook is None:  # checked in _reason_to_skip; the row is locked meanwhile
            raise DeliveryError("webhook removed", permanent=True)
        # The current URL and secret: a rotated secret applies to queued deliveries too.
        await self._webhook.post(
            url=webhook.url,
            secret=webhook.secret,
            body=dumps(delivery.payload),
            idempotency_key=delivery.idempotency_key,
        )
