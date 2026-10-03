from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from smarthome.modules.notifications.api.routes import GuestsExcluded
from smarthome.modules.notifications.domain.errors import (
    AlertNotFound,
    InvalidPreferences,
    InvalidWebhook,
    NotificationNotFound,
    NotificationsError,
    NoWebhook,
)
from smarthome.shared.http.problems import problem

STATUS: dict[type[NotificationsError], tuple[int, str]] = {
    AlertNotFound: (status.HTTP_404_NOT_FOUND, "No open alert with this id"),
    NotificationNotFound: (status.HTTP_404_NOT_FOUND, "Notification not found"),
    NoWebhook: (status.HTTP_404_NOT_FOUND, "No webhook set for this home"),
    InvalidWebhook: (status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid webhook URL"),
    InvalidPreferences: (status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid preferences"),
}


async def _notifications_error(_: Request, exc: Exception) -> JSONResponse:
    code, title = next(
        (v for k, v in STATUS.items() if isinstance(exc, k)),
        (status.HTTP_400_BAD_REQUEST, "Rejected"),
    )
    if code == status.HTTP_404_NOT_FOUND:
        return problem(code, title)
    return problem(code, title, str(exc) or None)


async def _guests_excluded(_: Request, __: Exception) -> JSONResponse:
    return problem(
        status.HTTP_403_FORBIDDEN, "Not allowed", "Guest passes do not include home alerts."
    )


def register(app: FastAPI) -> None:
    app.add_exception_handler(NotificationsError, _notifications_error)
    app.add_exception_handler(GuestsExcluded, _guests_excluded)
