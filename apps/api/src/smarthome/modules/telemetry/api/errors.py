from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from smarthome.modules.telemetry.domain.errors import InvalidRange
from smarthome.shared.http.problems import problem


async def _invalid_range(_: Request, exc: Exception) -> JSONResponse:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid time range", str(exc))


def register(app: FastAPI) -> None:
    app.add_exception_handler(InvalidRange, _invalid_range)
