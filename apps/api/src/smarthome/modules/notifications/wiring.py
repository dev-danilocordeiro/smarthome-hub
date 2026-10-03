"""Composition of the notifications module for an entrypoint. Only entrypoints import this."""

from datetime import timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.devices.public import DevicesService
from smarthome.modules.energy.public import EnergyService
from smarthome.modules.identity.public import IdentityService
from smarthome.modules.notifications.api import errors, routes
from smarthome.modules.notifications.api.routes import NotificationsModule
from smarthome.modules.notifications.application.alerts import AlertEngine
from smarthome.modules.notifications.application.dispatch import DeliveryDispatcher
from smarthome.modules.notifications.application.services import NotificationsService
from smarthome.modules.notifications.domain.model import Timing
from smarthome.modules.notifications.infrastructure.modules import (
    DevicesDirectory,
    EnergyBudgets,
    IdentityHomes,
)
from smarthome.modules.notifications.infrastructure.persistence import (
    PostgresNotificationsUnitOfWork,
)
from smarthome.modules.notifications.infrastructure.senders import (
    AddressGuard,
    HttpWebhookSender,
    SmtpEmailSender,
)
from smarthome.shared.clock import Clock
from smarthome.shared.config import Environment, Settings
from smarthome.shared.events import EventPublisher

__all__ = [
    "NotificationsModule",
    "build",
    "build_dispatcher",
    "build_engine",
    "mount",
    "webhook_client",
]

WEBHOOK_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def _timing(settings: Settings) -> Timing:
    return Timing(
        offline_grace=timedelta(seconds=settings.alert_offline_grace_s),
        emails_per_hour=settings.notification_emails_per_hour,
    )


def _guard(settings: Settings) -> AddressGuard:
    # Webhooks to the developer's own machine are the point in local and test runs.
    return AddressGuard(allow_private=settings.environment is not Environment.PRODUCTION)


def webhook_client() -> httpx.AsyncClient:
    """Redirects are not followed: a 3xx to an internal address would bypass the guard."""
    return httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT, follow_redirects=False)


def build(
    settings: Settings, *, engine: AsyncEngine, identity: IdentityService, clock: Clock
) -> NotificationsModule:
    service = NotificationsService(
        lambda: PostgresNotificationsUnitOfWork(engine),
        url_guard=_guard(settings),
        clock=clock,
        allow_http_webhooks=settings.environment is not Environment.PRODUCTION,
    )
    return NotificationsModule(service=service, identity=identity)


def build_engine(
    settings: Settings,
    *,
    engine: AsyncEngine,
    identity: IdentityService,
    devices: DevicesService,
    energy: EnergyService,
    clock: Clock,
    events: EventPublisher | None = None,
) -> AlertEngine:
    return AlertEngine(
        lambda: PostgresNotificationsUnitOfWork(engine),
        homes=IdentityHomes(identity),
        devices=DevicesDirectory(devices),
        budgets=EnergyBudgets(energy),
        clock=clock,
        timing=_timing(settings),
        events=events,
    )


def build_dispatcher(
    settings: Settings, *, engine: AsyncEngine, http: httpx.AsyncClient, clock: Clock
) -> DeliveryDispatcher:
    return DeliveryDispatcher(
        lambda: PostgresNotificationsUnitOfWork(engine),
        email=SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            sender=settings.smtp_sender,
            username=settings.smtp_username,
            password=(
                settings.smtp_password.get_secret_value() if settings.smtp_password else None
            ),
            starttls=settings.smtp_starttls,
        ),
        webhook=HttpWebhookSender(http, _guard(settings)),
        clock=clock,
        timing=_timing(settings),
    )


def mount(app: FastAPI) -> None:
    app.include_router(routes.router)
    errors.register(app)
