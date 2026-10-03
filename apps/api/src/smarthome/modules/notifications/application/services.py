"""What people do with alerts and notifications over HTTP."""

import secrets
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from smarthome.modules.notifications.application.ports import (
    NewDelivery,
    NotificationsUnitOfWorkFactory,
    UrlGuard,
)
from smarthome.modules.notifications.domain.errors import (
    AlertNotFound,
    NotificationNotFound,
    NoWebhook,
)
from smarthome.modules.notifications.domain.model import (
    Alert,
    AlertStatus,
    Channel,
    Notification,
    Preferences,
    Webhook,
)
from smarthome.modules.notifications.domain.webhook import validate_url
from smarthome.shared.audit import AuditEvent
from smarthome.shared.clock import Clock

SECRET_BYTES = 32


@dataclass(frozen=True, slots=True)
class SavedWebhook:
    webhook: Webhook
    secret_shown: str | None  # the secret, only when it was just created or rotated


class NotificationsService:
    def __init__(
        self,
        uow: NotificationsUnitOfWorkFactory,
        *,
        url_guard: UrlGuard,
        clock: Clock,
        allow_http_webhooks: bool,
    ) -> None:
        self._uow = uow
        self._guard = url_guard
        self._clock = clock
        self._allow_http = allow_http_webhooks

    # --- Alerts ---------------------------------------------------------------------------

    async def alerts(
        self,
        home_id: UUID,
        *,
        statuses: frozenset[AlertStatus],
        scope: frozenset[str] | None,
        limit: int,
    ) -> list[Alert]:
        async with self._uow() as uow:
            found = await uow.alerts.for_home(home_id, statuses=statuses, limit=limit)
        return [a for a in found if scope is None or a.device_id in scope]

    async def acknowledge(self, home_id: UUID, alert_id: UUID, *, by: str) -> Alert:
        """Someone has seen it and is on it. It stays open until the condition clears."""
        now = self._clock.now()
        async with self._uow() as uow:
            alert = await uow.alerts.get(home_id, alert_id)
            if alert is None or alert.status is not AlertStatus.OPEN:
                raise AlertNotFound(str(alert_id))
            acknowledged = await uow.alerts.acknowledge(alert_id, by=by, at=now)
            await uow.commit()
        return acknowledged

    # --- Inbox ----------------------------------------------------------------------------

    async def inbox(
        self,
        user_id: str,
        homes: list[UUID],
        *,
        unread_only: bool,
        limit: int,
        before: datetime | None,
    ) -> tuple[list[Notification], int]:
        """`homes` are the homes the user is a member of now: entries from a home they
        have left stay hidden."""
        async with self._uow() as uow:
            entries = await uow.inbox.for_user(
                user_id, homes, unread_only=unread_only, limit=limit, before=before
            )
            unread = await uow.inbox.unread_count(user_id, homes)
        return entries, unread

    async def unread(self, user_id: str, homes: list[UUID]) -> int:
        async with self._uow() as uow:
            return await uow.inbox.unread_count(user_id, homes)

    async def mark_read(self, user_id: str, notification_id: UUID) -> None:
        async with self._uow() as uow:
            if not await uow.inbox.mark_read(user_id, notification_id, at=self._clock.now()):
                raise NotificationNotFound(str(notification_id))
            await uow.commit()

    async def mark_all_read(self, user_id: str, homes: list[UUID]) -> int:
        async with self._uow() as uow:
            count = await uow.inbox.mark_all_read(user_id, homes, at=self._clock.now())
            await uow.commit()
        return count

    # --- Preferences ----------------------------------------------------------------------

    async def preferences(self, home_id: UUID, user_id: str) -> Preferences:
        async with self._uow() as uow:
            return (await uow.preferences.for_home(home_id)).get(user_id, Preferences())

    async def set_preferences(self, home_id: UUID, user_id: str, prefs: Preferences) -> None:
        async with self._uow() as uow:
            await uow.preferences.save(home_id, user_id, prefs, at=self._clock.now())
            await uow.commit()

    # --- Webhook --------------------------------------------------------------------------

    async def webhook(self, home_id: UUID) -> Webhook | None:
        async with self._uow() as uow:
            return await uow.webhooks.get(home_id)

    async def set_webhook(
        self, home_id: UUID, *, url: str, enabled: bool, rotate_secret: bool, actor: str
    ) -> SavedWebhook:
        validate_url(url, allow_http=self._allow_http)
        await self._guard.check(url)
        now = self._clock.now()
        async with self._uow() as uow:
            current = await uow.webhooks.get(home_id)
            new_secret = current is None or rotate_secret
            secret = (
                current.secret
                if current is not None and not rotate_secret
                else secrets.token_urlsafe(SECRET_BYTES)
            )
            webhook = Webhook(home_id, url, secret, enabled, actor, now)
            await uow.webhooks.save(webhook)
            await uow.audit.append(
                AuditEvent(
                    actor=actor,
                    action="notifications.webhook_set",
                    target_type="home",
                    target_id=str(home_id),
                    tenant_id=home_id,
                    # Never the secret; the URL may carry a token, so only its host.
                    details={
                        "host": urlsplit(url).hostname,
                        "enabled": enabled,
                        "secret_rotated": new_secret,
                    },
                ),
                occurred_at=now,
            )
            await uow.commit()
        return SavedWebhook(webhook, secret if new_secret else None)

    async def delete_webhook(self, home_id: UUID, *, actor: str) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            if not await uow.webhooks.delete(home_id):
                raise NoWebhook
            await uow.audit.append(
                AuditEvent(
                    actor=actor,
                    action="notifications.webhook_deleted",
                    target_type="home",
                    target_id=str(home_id),
                    tenant_id=home_id,
                ),
                occurred_at=now,
            )
            await uow.commit()

    async def test_webhook(self, home_id: UUID) -> str:
        """Queue a `ping` delivery; returns its idempotency key."""
        now = self._clock.now()
        key = f"ping:{home_id}:{secrets.token_hex(8)}"
        async with self._uow() as uow:
            webhook = await uow.webhooks.get(home_id)
            if webhook is None:
                raise NoWebhook
            await uow.deliveries.enqueue(
                [
                    NewDelivery(
                        home_id=home_id,
                        alert_id=None,
                        user_id=None,
                        channel=Channel.WEBHOOK,
                        target=webhook.url,
                        payload={"type": "ping", "home_id": str(home_id), "at": now.isoformat()},
                        idempotency_key=key,
                        next_attempt_at=now,
                    )
                ]
            )
            await uow.commit()
        return key
