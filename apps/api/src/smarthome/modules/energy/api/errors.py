from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from smarthome.modules.energy.api.routes import GuestsExcluded, NoTariff
from smarthome.modules.energy.domain.errors import (
    EnergyError,
    InvalidRange,
    InvalidTariff,
    VersionMismatch,
    VersionRequired,
)
from smarthome.shared.http.preconditions import PreconditionRequired
from smarthome.shared.http.problems import problem

STATUS: dict[type[EnergyError], tuple[int, str]] = {
    InvalidTariff: (status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid tariff"),
    InvalidRange: (status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid time range"),
    VersionMismatch: (status.HTTP_412_PRECONDITION_FAILED, "Edited version is not current"),
}


async def _energy_error(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, VersionRequired):
        # Same answer as any other edit without If-Match.
        handler = request.app.exception_handlers[PreconditionRequired]
        response: JSONResponse = await handler(request, exc)
        return response
    code, title = next(
        (v for k, v in STATUS.items() if isinstance(exc, k)),
        (status.HTTP_400_BAD_REQUEST, "Rejected"),
    )
    return problem(code, title, str(exc) or None)


async def _no_tariff(_: Request, __: Exception) -> JSONResponse:
    return problem(status.HTTP_404_NOT_FOUND, "No tariff set for this home")


async def _guests_excluded(_: Request, __: Exception) -> JSONResponse:
    return problem(
        status.HTTP_403_FORBIDDEN, "Not allowed", "Guest passes do not include home energy."
    )


def register(app: FastAPI) -> None:
    app.add_exception_handler(EnergyError, _energy_error)
    app.add_exception_handler(NoTariff, _no_tariff)
    app.add_exception_handler(GuestsExcluded, _guests_excluded)
