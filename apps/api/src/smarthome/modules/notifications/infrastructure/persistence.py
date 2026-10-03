"""Postgres adapters for the notifications ports (schema `notifications`)."""

import json
from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from smarthome.modules.notifications.application.ports import NewDelivery
from smarthome.modules.notifications.domain.model import (
    Alert,
    AlertKind,
    AlertStatus,
    Channel,
    Delivery,
    DeliveryStatus,
    Notification,
    NotificationEvent,
    Preferences,
    Severity,
    Webhook,
)
from smarthome.shared.infrastructure.audit import PostgresAuditLog

ALERT_COLUMNS = (
    "id, home_id, key, kind, severity, device_id, title, details, status, observed_at,"
    " created_at, due_at, opened_at, resolved_at, acknowledged_by, acknowledged_at"
)
INBOX_COLUMNS = "id, user_id, home_id, alert_id, event, severity, title, body, created_at, read_at"
DELIVERY_COLUMNS = (
    "id, home_id, alert_id, user_id, channel, target, payload, idempotency_key, attempts,"
    " next_attempt_at"
)
LIVE = "('pending', 'open')"


def _alert(row: Row[Any]) -> Alert:
    return Alert(
        id=row.id,
        home_id=row.home_id,
        key=row.key,
        kind=AlertKind(row.kind),
        severity=Severity.parse(row.severity),
        device_id=row.device_id,
        title=row.title,
        details=row.details,
        status=AlertStatus(row.status),
        observed_at=row.observed_at,
        created_at=row.created_at,
        due_at=row.due_at,
        opened_at=row.opened_at,
        resolved_at=row.resolved_at,
        acknowledged_by=row.acknowledged_by,
        acknowledged_at=row.acknowledged_at,
    )


def _notification(row: Row[Any]) -> Notification:
    return Notification(
        id=row.id,
        user_id=row.user_id,
        home_id=row.home_id,
        alert_id=row.alert_id,
        event=NotificationEvent(row.event),
        severity=Severity.parse(row.severity),
        title=row.title,
        body=row.body,
        created_at=row.created_at,
        read_at=row.read_at,
    )


class PostgresAlerts:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def insert(self, alert: Alert) -> bool:
        result = await self._conn.execute(
            text(
                f"INSERT INTO notifications.alerts ({ALERT_COLUMNS})"  # noqa: S608
                " VALUES (:id, :home, :key, :kind, :severity, :device, :title,"
                " CAST(:details AS jsonb), :status, :observed_at, :created_at, :due_at,"
                " :opened_at, NULL, NULL, NULL)"
                f" ON CONFLICT (home_id, key) WHERE status IN {LIVE} DO NOTHING"
            ),
            {
                "id": alert.id,
                "home": alert.home_id,
                "key": alert.key,
                "kind": alert.kind.value,
                "severity": alert.severity.label,
                "device": alert.device_id,
                "title": alert.title,
                "details": json.dumps(alert.details),
                "status": alert.status.value,
                "observed_at": alert.observed_at,
                "created_at": alert.created_at,
                "due_at": alert.due_at,
                "opened_at": alert.opened_at,
            },
        )
        return bool(result.rowcount)

    async def last_observed(self, home_id: UUID, key: str) -> datetime | None:
        found: datetime | None = await self._conn.scalar(
            text(
                "SELECT max(observed_at) FROM notifications.alerts"
                " WHERE home_id = :home AND key = :key"
            ),
            {"home": home_id, "key": key},
        )
        return found

    async def touch(self, home_id: UUID, key: str, observed_at: datetime) -> None:
        await self._conn.execute(
            text(
                "UPDATE notifications.alerts SET observed_at = greatest(observed_at, :at)"  # noqa: S608 - column list constant
                f" WHERE home_id = :home AND key = :key AND status IN {LIVE}"
            ),
            {"home": home_id, "key": key, "at": observed_at},
        )

    async def clear(
        self, home_id: UUID, key: str, *, at: datetime, observed_at: datetime
    ) -> Alert | None:
        # A pending alert is closed without ever having opened (opened_at stays NULL), so
        # its observation time still guards against stale events for the key.
        row = (
            await self._conn.execute(
                text(
                    "WITH before AS ("  # noqa: S608 - column list constant
                    f" SELECT {ALERT_COLUMNS} FROM notifications.alerts"
                    f" WHERE home_id = :home AND key = :key AND status IN {LIVE} FOR UPDATE)"
                    " UPDATE notifications.alerts a SET status = 'resolved', due_at = NULL,"
                    " resolved_at = :at, observed_at = greatest(a.observed_at, :observed_at)"
                    " FROM before WHERE a.id = before.id"
                    " RETURNING before.*"
                ),
                {"home": home_id, "key": key, "at": at, "observed_at": observed_at},
            )
        ).first()
        return _alert(row) if row else None

    async def claim_due(self, now: datetime, *, limit: int) -> list[Alert]:
        rows = await self._conn.execute(
            text(
                f"SELECT {ALERT_COLUMNS} FROM notifications.alerts"  # noqa: S608
                " WHERE status = 'pending' AND due_at <= :now ORDER BY due_at"
                " LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {"now": now, "limit": limit},
        )
        return [_alert(r) for r in rows]

    async def mark_open(self, alert_id: UUID, *, at: datetime) -> Alert:
        row = (
            await self._conn.execute(
                text(
                    "UPDATE notifications.alerts SET status = 'open', due_at = NULL,"  # noqa: S608 - column list constant
                    " opened_at = :at WHERE id = :id"
                    f" RETURNING {ALERT_COLUMNS}"
                ),
                {"id": alert_id, "at": at},
            )
        ).one()
        return _alert(row)

    async def get(self, home_id: UUID, alert_id: UUID) -> Alert | None:
        row = (
            await self._conn.execute(
                text(
                    f"SELECT {ALERT_COLUMNS} FROM notifications.alerts"  # noqa: S608
                    " WHERE id = :id AND home_id = :home"
                ),
                {"id": alert_id, "home": home_id},
            )
        ).first()
        return _alert(row) if row else None

    async def status_of(self, alert_id: UUID) -> AlertStatus | None:
        found = await self._conn.scalar(
            text("SELECT status FROM notifications.alerts WHERE id = :id"), {"id": alert_id}
        )
        return AlertStatus(found) if found else None

    async def for_home(
        self, home_id: UUID, *, statuses: frozenset[AlertStatus], limit: int
    ) -> list[Alert]:
        rows = await self._conn.execute(
            text(
                f"SELECT {ALERT_COLUMNS} FROM notifications.alerts"  # noqa: S608
                " WHERE home_id = :home AND status = ANY(:statuses)"
                " AND opened_at IS NOT NULL ORDER BY opened_at DESC LIMIT :limit"
            ),
            {"home": home_id, "statuses": [s.value for s in statuses], "limit": limit},
        )
        return [_alert(r) for r in rows]

    async def live_of_kind(self, home_id: UUID, kind: AlertKind) -> list[Alert]:
        rows = await self._conn.execute(
            text(
                f"SELECT {ALERT_COLUMNS} FROM notifications.alerts"  # noqa: S608
                f" WHERE home_id = :home AND kind = :kind AND status IN {LIVE}"
            ),
            {"home": home_id, "kind": kind.value},
        )
        return [_alert(r) for r in rows]

    async def acknowledge(self, alert_id: UUID, *, by: str, at: datetime) -> Alert:
        row = (
            await self._conn.execute(
                text(
                    "UPDATE notifications.alerts SET acknowledged_by = :by,"  # noqa: S608 - column list constant
                    " acknowledged_at = :at WHERE id = :id"
                    f" RETURNING {ALERT_COLUMNS}"
                ),
                {"id": alert_id, "by": by, "at": at},
            )
        ).one()
        return _alert(row)

    async def purge(self, *, resolved_before: datetime) -> int:
        result = await self._conn.execute(
            text(
                "DELETE FROM notifications.alerts"
                " WHERE status = 'resolved' AND resolved_at < :cutoff"
            ),
            {"cutoff": resolved_before},
        )
        return int(result.rowcount)


class PostgresInbox:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, notifications: list[Notification]) -> None:
        if not notifications:
            return
        await self._conn.execute(
            text(
                f"INSERT INTO notifications.inbox ({INBOX_COLUMNS})"  # noqa: S608
                " VALUES (:id, :user, :home, :alert, :event, :severity, :title, :body,"
                " :created_at, NULL) ON CONFLICT ON CONSTRAINT once_per_person DO NOTHING"
            ),
            [
                {
                    "id": n.id,
                    "user": n.user_id,
                    "home": n.home_id,
                    "alert": n.alert_id,
                    "event": n.event.value,
                    "severity": n.severity.label,
                    "title": n.title,
                    "body": n.body,
                    "created_at": n.created_at,
                }
                for n in notifications
            ],
        )

    async def for_user(
        self,
        user_id: str,
        homes: list[UUID],
        *,
        unread_only: bool,
        limit: int,
        before: datetime | None,
    ) -> list[Notification]:
        rows = await self._conn.execute(
            text(
                f"SELECT {INBOX_COLUMNS} FROM notifications.inbox"  # noqa: S608
                " WHERE user_id = :user AND home_id = ANY(:homes)"
                " AND (NOT :unread_only OR read_at IS NULL)"
                " AND (CAST(:before AS timestamptz) IS NULL OR created_at < :before)"
                " ORDER BY created_at DESC, id LIMIT :limit"
            ),
            {
                "user": user_id,
                "homes": homes,
                "unread_only": unread_only,
                "before": before,
                "limit": limit,
            },
        )
        return [_notification(r) for r in rows]

    async def unread_count(self, user_id: str, homes: list[UUID]) -> int:
        return int(
            await self._conn.scalar(
                text(
                    "SELECT count(*) FROM notifications.inbox"
                    " WHERE user_id = :user AND home_id = ANY(:homes) AND read_at IS NULL"
                ),
                {"user": user_id, "homes": homes},
            )
            or 0
        )

    async def mark_read(self, user_id: str, notification_id: UUID, *, at: datetime) -> bool:
        result = await self._conn.execute(
            text(
                "UPDATE notifications.inbox SET read_at = coalesce(read_at, :at)"
                " WHERE id = :id AND user_id = :user"
            ),
            {"id": notification_id, "user": user_id, "at": at},
        )
        return bool(result.rowcount)

    async def mark_all_read(self, user_id: str, homes: list[UUID], *, at: datetime) -> int:
        result = await self._conn.execute(
            text(
                "UPDATE notifications.inbox SET read_at = :at"
                " WHERE user_id = :user AND home_id = ANY(:homes) AND read_at IS NULL"
            ),
            {"user": user_id, "homes": homes, "at": at},
        )
        return int(result.rowcount)

    async def purge(self, *, created_before: datetime) -> int:
        result = await self._conn.execute(
            text("DELETE FROM notifications.inbox WHERE created_at < :cutoff"),
            {"cutoff": created_before},
        )
        return int(result.rowcount)


class PostgresDeliveries:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def enqueue(self, deliveries: list[NewDelivery]) -> None:
        if not deliveries:
            return
        await self._conn.execute(
            text(
                "INSERT INTO notifications.deliveries (home_id, alert_id, user_id, channel,"
                " target, payload, idempotency_key, status, next_attempt_at, created_at)"
                " VALUES (:home, :alert, :user, :channel, :target, CAST(:payload AS jsonb),"
                " :key, 'queued', :next, now())"
                " ON CONFLICT (idempotency_key) DO NOTHING"
            ),
            [
                {
                    "home": d.home_id,
                    "alert": d.alert_id,
                    "user": d.user_id,
                    "channel": d.channel.value,
                    "target": d.target,
                    "payload": json.dumps(d.payload),
                    "key": d.idempotency_key,
                    "next": d.next_attempt_at,
                }
                for d in deliveries
            ],
        )

    async def claim_due(self, now: datetime, *, limit: int) -> list[Delivery]:
        rows = await self._conn.execute(
            text(
                f"SELECT {DELIVERY_COLUMNS} FROM notifications.deliveries"  # noqa: S608
                " WHERE status = 'queued' AND next_attempt_at <= :now"
                " ORDER BY next_attempt_at LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {"now": now, "limit": limit},
        )
        return [
            Delivery(
                id=r.id,
                home_id=r.home_id,
                alert_id=r.alert_id,
                channel=Channel(r.channel),
                target=r.target,
                payload=r.payload,
                idempotency_key=r.idempotency_key,
                attempts=r.attempts,
                next_attempt_at=r.next_attempt_at,
                user_id=r.user_id,
            )
            for r in rows
        ]

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
        await self._conn.execute(
            text(
                "UPDATE notifications.deliveries SET status = :status, attempts = :attempts,"
                " last_error = :error, next_attempt_at = :next,"
                " settled_at = CASE WHEN :status = 'queued' THEN NULL"
                " ELSE CAST(:at AS timestamptz) END"
                " WHERE id = :id"
            ),
            {
                "id": delivery_id,
                "status": status.value,
                "attempts": attempts,
                "error": error,
                "next": next_attempt_at,
                "at": at,
            },
        )

    async def sent_since(self, channel: Channel, target: str, since: datetime) -> int:
        return int(
            await self._conn.scalar(
                text(
                    "SELECT count(*) FROM notifications.deliveries WHERE status = 'sent'"
                    " AND channel = :channel AND target = :target AND settled_at >= :since"
                ),
                {"channel": channel.value, "target": target, "since": since},
            )
            or 0
        )

    async def purge(self, *, settled_before: datetime) -> int:
        result = await self._conn.execute(
            text(
                "DELETE FROM notifications.deliveries"
                " WHERE status <> 'queued' AND settled_at < :cutoff"
            ),
            {"cutoff": settled_before},
        )
        return int(result.rowcount)


class PostgresPreferences:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def for_home(self, home_id: UUID) -> dict[str, Preferences]:
        rows = await self._conn.execute(
            text(
                "SELECT user_id, email_enabled, min_email_severity, quiet_start, quiet_end"
                " FROM notifications.preferences WHERE home_id = :home"
            ),
            {"home": home_id},
        )
        return {
            r.user_id: Preferences(
                email_enabled=r.email_enabled,
                min_email_severity=Severity.parse(r.min_email_severity),
                quiet_start=r.quiet_start,
                quiet_end=r.quiet_end,
            )
            for r in rows
        }

    async def save(self, home_id: UUID, user_id: str, prefs: Preferences, *, at: datetime) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO notifications.preferences (home_id, user_id, email_enabled,"
                " min_email_severity, quiet_start, quiet_end, updated_at)"
                " VALUES (:home, :user, :email, :severity, :start, :end, :at)"
                " ON CONFLICT (home_id, user_id) DO UPDATE SET"
                " email_enabled = EXCLUDED.email_enabled,"
                " min_email_severity = EXCLUDED.min_email_severity,"
                " quiet_start = EXCLUDED.quiet_start, quiet_end = EXCLUDED.quiet_end,"
                " updated_at = EXCLUDED.updated_at"
            ),
            {
                "home": home_id,
                "user": user_id,
                "email": prefs.email_enabled,
                "severity": prefs.min_email_severity.label,
                "start": prefs.quiet_start,
                "end": prefs.quiet_end,
                "at": at,
            },
        )


class PostgresWebhooks:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get(self, home_id: UUID) -> Webhook | None:
        row = (
            await self._conn.execute(
                text(
                    "SELECT home_id, url, secret, enabled, updated_by, updated_at"
                    " FROM notifications.webhooks WHERE home_id = :home"
                ),
                {"home": home_id},
            )
        ).first()
        if row is None:
            return None
        return Webhook(
            row.home_id, row.url, row.secret, row.enabled, row.updated_by, row.updated_at
        )

    async def save(self, webhook: Webhook) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO notifications.webhooks"
                " (home_id, url, secret, enabled, updated_by, updated_at)"
                " VALUES (:home, :url, :secret, :enabled, :by, :at)"
                " ON CONFLICT (home_id) DO UPDATE SET url = EXCLUDED.url,"
                " secret = EXCLUDED.secret, enabled = EXCLUDED.enabled,"
                " updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at"
            ),
            {
                "home": webhook.home_id,
                "url": webhook.url,
                "secret": webhook.secret,
                "enabled": webhook.enabled,
                "by": webhook.updated_by,
                "at": webhook.updated_at,
            },
        )

    async def delete(self, home_id: UUID) -> bool:
        result = await self._conn.execute(
            text("DELETE FROM notifications.webhooks WHERE home_id = :home"), {"home": home_id}
        )
        return bool(result.rowcount)


class PostgresNotificationsUnitOfWork:
    alerts: PostgresAlerts
    inbox: PostgresInbox
    deliveries: PostgresDeliveries
    preferences: PostgresPreferences
    webhooks: PostgresWebhooks
    audit: PostgresAuditLog

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn: AsyncConnection | None = None
        self._tx: AsyncTransaction | None = None

    async def __aenter__(self) -> Self:
        self._conn = await self._engine.connect()
        self._tx = await self._conn.begin()
        self.alerts = PostgresAlerts(self._conn)
        self.inbox = PostgresInbox(self._conn)
        self.deliveries = PostgresDeliveries(self._conn)
        self.preferences = PostgresPreferences(self._conn)
        self.webhooks = PostgresWebhooks(self._conn)
        self.audit = PostgresAuditLog(self._conn)
        return self

    async def commit(self) -> None:
        if self._tx is None:
            raise RuntimeError("unit of work is not active")
        await self._tx.commit()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._tx is not None and self._tx.is_active:
                await self._tx.rollback()
        finally:
            if self._conn is not None:
                await self._conn.close()
            self._conn = None
            self._tx = None
