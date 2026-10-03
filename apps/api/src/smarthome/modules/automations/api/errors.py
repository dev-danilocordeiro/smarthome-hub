from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from smarthome.modules.automations.api.routes import (
    NotAllowed,
    ReauthenticationRequired,
)
from smarthome.modules.automations.domain.errors import (
    AutomationNotFound,
    AutomationsError,
    InvalidDefinition,
    InvalidRange,
    NameTaken,
    SceneInUse,
    SceneNotFound,
    VersionMismatch,
)
from smarthome.shared.http.problems import problem

REAUTHENTICATION_REQUIRED = "urn:smarthome:problem:reauthentication-required"
INVALID_DEFINITION = "urn:smarthome:problem:invalid-automation"

STATUS: dict[type[AutomationsError], tuple[int, str]] = {
    AutomationNotFound: (status.HTTP_404_NOT_FOUND, "Automation not found"),
    SceneNotFound: (status.HTTP_404_NOT_FOUND, "Scene not found"),
    InvalidDefinition: (status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid definition"),
    InvalidRange: (status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid time range"),
    NameTaken: (status.HTTP_409_CONFLICT, "Name already in use in this home"),
    SceneInUse: (status.HTTP_409_CONFLICT, "Scene is used by automations"),
    VersionMismatch: (status.HTTP_412_PRECONDITION_FAILED, "Edited version is not current"),
}


async def _automations_error(_: Request, exc: Exception) -> JSONResponse:
    code, title = next(
        (v for k, v in STATUS.items() if isinstance(exc, k)),
        (status.HTTP_400_BAD_REQUEST, "Rejected"),
    )
    if isinstance(exc, AutomationNotFound | SceneNotFound):
        return problem(code, title)
    if isinstance(exc, InvalidDefinition):
        # `path` points into the submitted JSON (e.g. "triggers/0/value") for editors.
        return problem(
            code, title, str(exc), type_=INVALID_DEFINITION, extensions={"path": exc.path}
        )
    return problem(code, title, str(exc))


async def _not_allowed(_: Request, exc: Exception) -> JSONResponse:
    return problem(status.HTTP_403_FORBIDDEN, "Not allowed", str(exc))


async def _reauthenticate(request: Request, _: Exception) -> JSONResponse:
    settings = request.app.state.automations.settings
    return problem(
        status.HTTP_401_UNAUTHORIZED,
        "Sign in again to automate locks and cameras",
        "This change needs a sign-in from the last few minutes.",
        type_=REAUTHENTICATION_REQUIRED,
        extensions={"login_url": f"{settings.bff_public_url}/auth/login?reauth=true"},
    )


def register(app: FastAPI) -> None:
    app.add_exception_handler(AutomationsError, _automations_error)
    app.add_exception_handler(NotAllowed, _not_allowed)
    app.add_exception_handler(ReauthenticationRequired, _reauthenticate)
