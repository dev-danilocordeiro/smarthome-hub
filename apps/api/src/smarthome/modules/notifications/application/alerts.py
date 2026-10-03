"""The alert engine (worker): device events, grace timers and energy budgets in; alerts,
inbox entries and queued deliveries out (ADR 0013).

Safe with several workers: one live alert per key is enforced by a unique index, so of
two workers raising the same alert only one inserts it and notifies; resolving is an
UPDATE ... WHERE status = 'open', so only one of them sees the row change.
"""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import structlog
from opentelemetry import metrics

from smarthome.modules.notifications.application.ports import (
    Audience,
    Budgets,
    Devices,
    Homes,
    NewDelivery,
    NotificationsUnitOfWork,
    NotificationsUnitOfWorkFactory,
)
from smarthome.modules.notifications.domain.model import (
    Alert,
    AlertKind,
    AlertStatus,
    Channel,
    Clear,
    Notification,
    NotificationEvent,
    Preferences,
    Raise,
    Timing,
)
from smarthome.modules.notifications.domain.rules import email_at, on_budget, on_device_event
from smarthome.shared.clock import Clock
from smarthome.shared.events import (
    DeviceEvent,
    DeviceEventKind,
    EventPublisher,
    NullEventPublisher,
)

log = structlog.get_logger(__name__)
meter = metrics.get_meter(__name__)

transitions_counter = meter.create_counter(
    "smarthome.alerts.transitions",
    unit="{alert}",
    description="Alerts opened and resolved, by kind and transition.",
)

DUE_BATCH = 100


def alert_payload(alert: Alert, event: NotificationEvent) -> dict[str, Any]:
    """The JSON a webhook receives; also the shape of the live `alert` message."""
    return {
        "type": f"alert.{event.value}",
        "alert": {
            "id": str(alert.id),
            "home_id": str(alert.home_id),
            "key": alert.key,
            "kind": alert.kind.value,
            "severity": alert.severity.label,
            "status": alert.status.value,
            "device_id": alert.device_id,
            "title": alert.title,
            "details": alert.details,
            "opened_at": alert.opened_at.isoformat() if alert.opened_at else None,
            "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
        },
    }


def message(alert: Alert, event: NotificationEvent) -> tuple[str, str]:
    """(title, body) of an inbox entry or email."""
    if event is NotificationEvent.RESOLVED:
        return f"Resolved: {alert.title}", f"{alert.title} - no longer the case."
    details = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in alert.details.items())
    return alert.title, details or alert.title


class AlertEngine:
    def __init__(
        self,
        uow: NotificationsUnitOfWorkFactory,
        *,
        homes: Homes,
        devices: Devices,
        budgets: Budgets,
        clock: Clock,
        timing: Timing,
        events: EventPublisher | None = None,
    ) -> None:
        self._uow = uow
        self._homes = homes
        self._devices = devices
        self._budgets = budgets
        self._clock = clock
        self._timing = timing
        self._events = events or NullEventPublisher()

    # --- Inputs ---------------------------------------------------------------------------

    async def handle_event(self, _: str, event: DeviceEvent) -> int:
        """Consumer group handler. Returns how many alerts changed."""
        relevant = event.kind is DeviceEventKind.PRESENCE or (
            event.kind is DeviceEventKind.TELEMETRY and "battery_pct" in event.data
        )
        if not relevant:  # most telemetry: skip without touching the database
            return 0
        device = await self._devices.info(event.home_id, event.device_id)
        if device is None:
            return 0
        return await self.apply(event.home_id, on_device_event(event, device, self._timing))

    async def fire_due(self) -> int:
        """Open pending alerts whose grace period ended. An offline alert whose device
        came back in the meantime (and whose `online` event was lost) is dropped."""
        now = self._clock.now()
        opened: list[Alert] = []
        dropped = 0
        async with self._uow() as uow:
            for alert in await uow.alerts.claim_due(now, limit=DUE_BATCH):
                if alert.kind is AlertKind.DEVICE_OFFLINE and alert.device_id is not None:
                    online = await self._devices.is_online(alert.home_id, alert.device_id)
                    if online is not False:
                        await uow.alerts.clear(
                            alert.home_id, alert.key, at=now, observed_at=alert.observed_at
                        )
                        dropped += 1
                        continue
                opened_alert = await uow.alerts.mark_open(alert.id, at=now)
                await self._notify(uow, opened_alert, NotificationEvent.OPENED, now=now)
                opened.append(opened_alert)
            await uow.commit()
        for alert in opened:
            await self._announce(alert, NotificationEvent.OPENED)
        return len(opened) + dropped

    async def check_budgets(self) -> int:
        now = self._clock.now()
        changed = 0
        for budget in await self._budgets.budgets():
            intents: list[Raise | Clear] = list(
                on_budget(
                    month_start=budget.month_start,
                    used_kwh=budget.used_kwh,
                    budget_kwh=budget.budget_kwh,
                    now=now,
                    timing=self._timing,
                )
            )
            # A new month starts from zero: last month's budget alerts are over.
            month = f"{budget.month_start:%Y-%m}"
            async with self._uow() as uow:
                live = await uow.alerts.live_of_kind(budget.home_id, AlertKind.ENERGY_BUDGET)
            intents += [Clear(a.key, now) for a in live if a.details.get("month") != month]
            changed += await self.apply(budget.home_id, intents)
        return changed

    # --- Transitions ----------------------------------------------------------------------

    async def apply(self, home_id: UUID, intents: list[Raise | Clear]) -> int:
        changed = 0
        for intent in intents:
            now = self._clock.now()
            async with self._uow() as uow:
                last = await uow.alerts.last_observed(home_id, intent.key)
                if last is not None and intent.observed_at < last:
                    continue  # older than what the alert already reflects
                if isinstance(intent, Raise):
                    alert, event = await self._raise(uow, home_id, intent, now=now)
                else:
                    alert, event = await self._clear(uow, home_id, intent, now=now)
                if alert is not None and event is not None:
                    await self._notify(uow, alert, event, now=now)
                await uow.commit()
            if alert is not None and event is not None:
                changed += 1
                await self._announce(alert, event)
        return changed

    async def _raise(
        self, uow: NotificationsUnitOfWork, home_id: UUID, intent: Raise, *, now: datetime
    ) -> tuple[Alert | None, NotificationEvent | None]:
        pending = intent.due_at is not None and intent.due_at > now
        alert = Alert(
            id=uuid4(),
            home_id=home_id,
            key=intent.key,
            kind=intent.kind,
            severity=intent.severity,
            device_id=intent.device_id,
            title=intent.title,
            details=intent.details,
            status=AlertStatus.PENDING if pending else AlertStatus.OPEN,
            observed_at=intent.observed_at,
            created_at=now,
            due_at=intent.due_at if pending else None,
            opened_at=None if pending else now,
        )
        if not await uow.alerts.insert(alert):
            await uow.alerts.touch(home_id, intent.key, intent.observed_at)
            return None, None
        return (None, None) if pending else (alert, NotificationEvent.OPENED)

    async def _clear(
        self, uow: NotificationsUnitOfWork, home_id: UUID, intent: Clear, *, now: datetime
    ) -> tuple[Alert | None, NotificationEvent | None]:
        before = await uow.alerts.clear(home_id, intent.key, at=now, observed_at=intent.observed_at)
        if before is None or before.status is not AlertStatus.OPEN:
            return None, None  # nothing live, or a pending alert nobody was told about
        resolved = replace(
            before, status=AlertStatus.RESOLVED, resolved_at=now, observed_at=intent.observed_at
        )
        return resolved, NotificationEvent.RESOLVED

    # --- Fan-out --------------------------------------------------------------------------

    async def _notify(
        self, uow: NotificationsUnitOfWork, alert: Alert, event: NotificationEvent, *, now: datetime
    ) -> None:
        """Inbox entries for every member, emails per their preferences (new alerts
        only), and the home's webhook (both transitions). Same transaction as the alert:
        a transition is never recorded without its notifications, or the reverse."""
        audience = await self._homes.audience(alert.home_id) or Audience("UTC", ())
        preferences = await uow.preferences.for_home(alert.home_id)
        title, body = message(alert, event)
        await uow.inbox.add(
            [
                Notification(
                    id=uuid4(),
                    user_id=r.user_id,
                    home_id=alert.home_id,
                    alert_id=alert.id,
                    event=event,
                    severity=alert.severity,
                    title=title,
                    body=body,
                    created_at=now,
                )
                for r in audience.recipients
            ]
        )
        deliveries: list[NewDelivery] = []
        if event is NotificationEvent.OPENED:
            tz = ZoneInfo(audience.timezone)
            for recipient in audience.recipients:
                prefs = preferences.get(recipient.user_id, Preferences())
                send_at = email_at(prefs, alert.severity, now=now, tz=tz)
                if recipient.email is None or send_at is None:
                    continue
                deliveries.append(
                    NewDelivery(
                        home_id=alert.home_id,
                        alert_id=alert.id,
                        user_id=recipient.user_id,
                        channel=Channel.EMAIL,
                        target=recipient.email,
                        payload={"subject": f"[{alert.severity.label}] {title}", "body": body},
                        idempotency_key=f"{alert.id}:{event.value}:email:{recipient.user_id}",
                        next_attempt_at=send_at,
                    )
                )
        webhook = await uow.webhooks.get(alert.home_id)
        if webhook is not None and webhook.enabled:
            deliveries.append(
                NewDelivery(
                    home_id=alert.home_id,
                    alert_id=alert.id,
                    user_id=None,
                    channel=Channel.WEBHOOK,
                    target=webhook.url,
                    payload=alert_payload(alert, event),
                    idempotency_key=f"{alert.id}:{event.value}:webhook",
                    next_attempt_at=now,
                )
            )
        await uow.deliveries.enqueue(deliveries)
        transitions_counter.add(1, {"kind": alert.kind.value, "transition": event.value})
        log.info(
            "alert_" + event.value,
            alert_id=str(alert.id),
            home_id=str(alert.home_id),
            key=alert.key,
            recipients=len(audience.recipients),
            deliveries=len(deliveries),
        )

    async def _announce(self, alert: Alert, event: NotificationEvent) -> None:
        """Tell live clients (after commit; best effort)."""
        payload = alert_payload(alert, event)["alert"]
        await self._events.publish(
            DeviceEvent(
                alert.home_id,
                alert.device_id or "",
                DeviceEventKind.ALERT,
                alert.resolved_at or alert.opened_at or alert.created_at,
                {
                    "alert_id": payload["id"],
                    "kind": payload["kind"],
                    "severity": payload["severity"],
                    "status": payload["status"],
                    "title": payload["title"],
                },
            )
        )

    async def purge(self, *, older_than: timedelta) -> int:
        cutoff = self._clock.now() - older_than
        async with self._uow() as uow:
            purged = await uow.alerts.purge(resolved_before=cutoff)
            purged += await uow.inbox.purge(created_before=cutoff)
            purged += await uow.deliveries.purge(settled_before=cutoff)
            await uow.commit()
        return purged


def dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
