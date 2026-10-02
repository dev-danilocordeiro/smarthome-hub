from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from smarthome.modules.devices.api.routes import TooManyRequests
from smarthome.modules.devices.domain.errors import (
    DeviceNotFound,
    DevicesError,
    InvalidPairingCode,
    InvalidTransition,
    UnsupportedState,
)
from smarthome.modules.devices.infrastructure.broker_admin import BrokerAdminError
from smarthome.shared.http.problems import problem

STATUS: dict[type[DevicesError], tuple[int, str]] = {
    DeviceNotFound: (status.HTTP_404_NOT_FOUND, "Device not found"),
    InvalidPairingCode: (status.HTTP_410_GONE, "Pairing code is not valid"),
    InvalidTransition: (status.HTTP_409_CONFLICT, "Not possible in the device's current status"),
    UnsupportedState: (status.HTTP_422_UNPROCESSABLE_CONTENT, "Unsupported device state"),
}


async def _devices_error(_: Request, exc: Exception) -> JSONResponse:
    code, title = next(
        (v for k, v in STATUS.items() if isinstance(exc, k)),
        (status.HTTP_400_BAD_REQUEST, "Rejected"),
    )
    detail = None if isinstance(exc, DeviceNotFound) else str(exc)
    return problem(code, title, detail)


async def _too_many(_: Request, __: Exception) -> JSONResponse:
    response = problem(status.HTTP_429_TOO_MANY_REQUESTS, "Too many pairing attempts")
    response.headers["Retry-After"] = "60"
    return response


async def _broker_unavailable(_: Request, __: Exception) -> JSONResponse:
    return problem(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Device broker unavailable",
        "The change was not applied. Try again shortly.",
    )


def register(app: FastAPI) -> None:
    app.add_exception_handler(DevicesError, _devices_error)
    app.add_exception_handler(TooManyRequests, _too_many)
    app.add_exception_handler(BrokerAdminError, _broker_unavailable)
