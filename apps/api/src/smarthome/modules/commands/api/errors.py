from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from smarthome.modules.commands.api.routes import NotAllowed, ReauthenticationRequired
from smarthome.modules.commands.domain.errors import (
    CommandNotFound,
    CommandsError,
    InvalidCommand,
    TargetNotFound,
    TargetUnavailable,
)
from smarthome.shared.http.problems import problem

REAUTHENTICATION_REQUIRED = "urn:smarthome:problem:reauthentication-required"

STATUS: dict[type[CommandsError], tuple[int, str]] = {
    CommandNotFound: (status.HTTP_404_NOT_FOUND, "Command not found"),
    TargetNotFound: (status.HTTP_404_NOT_FOUND, "Device not found"),
    TargetUnavailable: (status.HTTP_409_CONFLICT, "Device does not accept commands right now"),
    InvalidCommand: (status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid command"),
}


async def _commands_error(_: Request, exc: Exception) -> JSONResponse:
    code, title = next(
        (v for k, v in STATUS.items() if isinstance(exc, k)),
        (status.HTTP_400_BAD_REQUEST, "Rejected"),
    )
    detail = None if isinstance(exc, CommandNotFound | TargetNotFound) else str(exc)
    return problem(code, title, detail)


async def _not_allowed(_: Request, exc: Exception) -> JSONResponse:
    return problem(status.HTTP_403_FORBIDDEN, "Not allowed", str(exc))


async def _reauthenticate(request: Request, _: Exception) -> JSONResponse:
    settings = request.app.state.commands.settings
    return problem(
        status.HTTP_401_UNAUTHORIZED,
        "Sign in again to operate locks and cameras",
        "This action needs a sign-in from the last few minutes.",
        type_=REAUTHENTICATION_REQUIRED,
        # The client appends `&return_to=<path>` to come back to where it was.
        extensions={"login_url": f"{settings.bff_public_url}/auth/login?reauth=true"},
    )


def register(app: FastAPI) -> None:
    app.add_exception_handler(CommandsError, _commands_error)
    app.add_exception_handler(NotAllowed, _not_allowed)
    app.add_exception_handler(ReauthenticationRequired, _reauthenticate)
