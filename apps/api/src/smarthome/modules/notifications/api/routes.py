from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from smarthome.modules.identity.public import (
    CurrentPrincipal,
    HomeAccess,
    IdentityService,
    Permission,
    Principal,
    require_home_access,
)
from smarthome.modules.notifications.application.services import NotificationsService
from smarthome.modules.notifications.domain.errors import InvalidPreferences, NoWebhook
from smarthome.modules.notifications.domain.model import (
    Alert,
    AlertStatus,
    Notification,
    Preferences,
    Severity,
    Webhook,
)

router = APIRouter(tags=["notifications"])

SeverityLabel = Literal["info", "warning", "critical"]


class GuestsExcluded(Exception):
    pass


@dataclass(frozen=True, slots=True)
class NotificationsModule:
    service: NotificationsService
    identity: IdentityService


def notifications_module(request: Request) -> NotificationsModule:
    module: NotificationsModule = request.app.state.notifications
    return module


Notifications = Annotated[NotificationsModule, Depends(notifications_module)]
CanView = Annotated[HomeAccess, Depends(require_home_access(Permission.VIEW_HOME))]
CanManageMembers = Annotated[HomeAccess, Depends(require_home_access(Permission.MANAGE_MEMBERS))]


def household_only(access: HomeAccess) -> None:
    if access.membership.device_scope is not None:
        raise GuestsExcluded


async def member_homes(module: NotificationsModule, principal: Principal) -> list[UUID]:
    return [access.home.id for access in await module.identity.my_homes(principal)]


# --- Schemas ---------------------------------------------------------------------------


class AlertOut(BaseModel):
    id: UUID
    kind: str
    severity: SeverityLabel
    status: AlertStatus
    device_id: str | None
    title: str
    details: dict[str, Any]
    opened_at: datetime | None
    resolved_at: datetime | None
    acknowledged_by: str | None
    acknowledged_at: datetime | None


class NotificationOut(BaseModel):
    id: UUID
    home_id: UUID
    alert_id: UUID | None
    event: str
    severity: SeverityLabel
    title: str
    body: str
    created_at: datetime
    read_at: datetime | None


class InboxOut(BaseModel):
    items: list[NotificationOut]
    unread: int


class UnreadOut(BaseModel):
    unread: int


class MarkedOut(BaseModel):
    marked: int


class PreferencesIn(BaseModel):
    email_enabled: bool = True
    min_email_severity: SeverityLabel = "warning"
    quiet_start: int | None = Field(default=None, ge=0, le=23, description="Hour, home time")
    quiet_end: int | None = Field(default=None, ge=0, le=23)


class PreferencesOut(PreferencesIn):
    pass


class WebhookIn(BaseModel):
    url: str = Field(max_length=500, examples=["https://example.com/hooks/smarthome"])
    enabled: bool = True
    rotate_secret: bool = False


class WebhookOut(BaseModel):
    url: str
    enabled: bool
    updated_by: str
    updated_at: datetime
    secret: str | None = Field(
        default=None,
        description="Only in the response that created or rotated it. Store it: it signs"
        " every delivery (X-Smarthome-Signature) and is never shown again.",
    )


class QueuedOut(BaseModel):
    idempotency_key: str


class AlertFilter(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ALL = "all"


FILTERS = {
    AlertFilter.OPEN: frozenset({AlertStatus.OPEN}),
    AlertFilter.RESOLVED: frozenset({AlertStatus.RESOLVED}),
    AlertFilter.ALL: frozenset({AlertStatus.OPEN, AlertStatus.RESOLVED}),
}


def _alert_out(alert: Alert) -> AlertOut:
    return AlertOut(
        id=alert.id,
        kind=alert.kind.value,
        severity=alert.severity.label,  # type: ignore[arg-type]
        status=alert.status,
        device_id=alert.device_id,
        title=alert.title,
        details=alert.details,
        opened_at=alert.opened_at,
        resolved_at=alert.resolved_at,
        acknowledged_by=alert.acknowledged_by,
        acknowledged_at=alert.acknowledged_at,
    )


def _notification_out(n: Notification) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        home_id=n.home_id,
        alert_id=n.alert_id,
        event=n.event.value,
        severity=n.severity.label,  # type: ignore[arg-type]
        title=n.title,
        body=n.body,
        created_at=n.created_at,
        read_at=n.read_at,
    )


def _preferences_out(p: Preferences) -> PreferencesOut:
    return PreferencesOut(
        email_enabled=p.email_enabled,
        min_email_severity=p.min_email_severity.label,  # type: ignore[arg-type]
        quiet_start=p.quiet_start,
        quiet_end=p.quiet_end,
    )


def _webhook_out(w: Webhook, secret: str | None = None) -> WebhookOut:
    return WebhookOut(
        url=w.url,
        enabled=w.enabled,
        updated_by=w.updated_by,
        updated_at=w.updated_at,
        secret=secret,
    )


# --- Alerts ----------------------------------------------------------------------------


@router.get("/homes/{home_id}/alerts")
async def list_alerts(
    access: CanView,
    module: Notifications,
    status_: Annotated[AlertFilter, Query(alias="status")] = AlertFilter.OPEN,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AlertOut]:
    """Alerts that have opened (newest first). A guest sees those of their devices."""
    alerts = await module.service.alerts(
        access.home.id,
        statuses=FILTERS[status_],
        scope=access.membership.device_scope,
        limit=limit,
    )
    return [_alert_out(a) for a in alerts]


@router.post("/homes/{home_id}/alerts/{alert_id}/acknowledge")
async def acknowledge(alert_id: UUID, access: CanView, module: Notifications) -> AlertOut:
    """Say you are on it. The alert stays open until whatever caused it clears."""
    household_only(access)
    alert = await module.service.acknowledge(access.home.id, alert_id, by=access.membership.user_id)
    return _alert_out(alert)


# --- Inbox -----------------------------------------------------------------------------


@router.get("/me/notifications")
async def inbox(
    principal: CurrentPrincipal,
    module: Notifications,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    before: datetime | None = None,
) -> InboxOut:
    """Your notifications across your homes, newest first. Page with `before` (the
    `created_at` of the last item you have)."""
    entries, unread = await module.service.inbox(
        principal.user_id,
        await member_homes(module, principal),
        unread_only=unread_only,
        limit=limit,
        before=before,
    )
    return InboxOut(items=[_notification_out(n) for n in entries], unread=unread)


@router.get("/me/notifications/unread-count")
async def unread_count(principal: CurrentPrincipal, module: Notifications) -> UnreadOut:
    homes = await member_homes(module, principal)
    return UnreadOut(unread=await module.service.unread(principal.user_id, homes))


@router.post("/me/notifications/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    notification_id: UUID, principal: CurrentPrincipal, module: Notifications
) -> Response:
    await module.service.mark_read(principal.user_id, notification_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/me/notifications/read-all")
async def mark_all_read(principal: CurrentPrincipal, module: Notifications) -> MarkedOut:
    homes = await member_homes(module, principal)
    return MarkedOut(marked=await module.service.mark_all_read(principal.user_id, homes))


# --- Preferences -----------------------------------------------------------------------


@router.get("/homes/{home_id}/notification-preferences")
async def get_preferences(access: CanView, module: Notifications) -> PreferencesOut:
    """Your own preferences for this home (defaults until you change them)."""
    household_only(access)
    prefs = await module.service.preferences(access.home.id, access.membership.user_id)
    return _preferences_out(prefs)


@router.put("/homes/{home_id}/notification-preferences")
async def put_preferences(
    body: PreferencesIn, access: CanView, module: Notifications
) -> PreferencesOut:
    household_only(access)
    try:
        prefs = Preferences(
            email_enabled=body.email_enabled,
            min_email_severity=Severity.parse(body.min_email_severity),
            quiet_start=body.quiet_start,
            quiet_end=body.quiet_end,
        )
    except ValueError as exc:
        raise InvalidPreferences(str(exc)) from exc
    await module.service.set_preferences(access.home.id, access.membership.user_id, prefs)
    return _preferences_out(prefs)


# --- Webhook ---------------------------------------------------------------------------


@router.get("/homes/{home_id}/webhook", responses={404: {"description": "No webhook"}})
async def get_webhook(access: CanManageMembers, module: Notifications) -> WebhookOut:
    webhook = await module.service.webhook(access.home.id)
    if webhook is None:
        raise NoWebhook
    return _webhook_out(webhook)


@router.put("/homes/{home_id}/webhook")
async def put_webhook(
    body: WebhookIn, access: CanManageMembers, module: Notifications
) -> WebhookOut:
    """Where to POST alert transitions for this home. The response that creates the
    webhook (or rotates its secret) is the only one that includes the secret."""
    saved = await module.service.set_webhook(
        access.home.id,
        url=body.url,
        enabled=body.enabled,
        rotate_secret=body.rotate_secret,
        actor=access.membership.user_id,
    )
    return _webhook_out(saved.webhook, saved.secret_shown)


@router.delete("/homes/{home_id}/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(access: CanManageMembers, module: Notifications) -> Response:
    await module.service.delete_webhook(access.home.id, actor=access.membership.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/homes/{home_id}/webhook/test", status_code=status.HTTP_202_ACCEPTED)
async def test_webhook(access: CanManageMembers, module: Notifications) -> QueuedOut:
    """Queue a signed `ping` delivery to the webhook."""
    return QueuedOut(idempotency_key=await module.service.test_webhook(access.home.id))
