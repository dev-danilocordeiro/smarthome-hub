from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from smarthome.modules.identity.api.dependencies import CsrfRejected, NotAuthenticated
from smarthome.modules.identity.domain.errors import (
    AccessDenied,
    AlreadyMember,
    HomeNotFound,
    IdentityError,
    InvalidInvitation,
    InvalidMembership,
    LastOwner,
)
from smarthome.modules.identity.infrastructure.sessions import SessionUnavailable
from smarthome.shared.http.problems import problem

STATUS: dict[type[IdentityError], tuple[int, str]] = {
    HomeNotFound: (status.HTTP_404_NOT_FOUND, "Home not found"),
    AccessDenied: (status.HTTP_403_FORBIDDEN, "Not allowed"),
    InvalidInvitation: (status.HTTP_410_GONE, "Invitation is no longer valid"),
    AlreadyMember: (status.HTTP_409_CONFLICT, "Already a member"),
    LastOwner: (status.HTTP_409_CONFLICT, "A home needs at least one owner"),
    InvalidMembership: (status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid membership"),
}


async def _identity_error(_: Request, exc: Exception) -> JSONResponse:
    code, title = next(
        (v for k, v in STATUS.items() if isinstance(exc, k)),
        (status.HTTP_400_BAD_REQUEST, "Request rejected"),
    )
    # HomeNotFound must not echo what was asked for: it doubles as "you are not a member".
    detail = None if isinstance(exc, HomeNotFound) else str(exc)
    return problem(code, title, detail)


async def _session_unavailable(_: Request, __: Exception) -> JSONResponse:
    return problem(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Sign-in service unavailable",
        "Your session could not be verified right now. Try again shortly.",
    )


async def _not_authenticated(_: Request, __: Exception) -> JSONResponse:
    return problem(status.HTTP_401_UNAUTHORIZED, "Not signed in")


async def _csrf_rejected(_: Request, exc: Exception) -> JSONResponse:
    return problem(status.HTTP_403_FORBIDDEN, "Request blocked", str(exc))


def register(app: FastAPI) -> None:
    app.add_exception_handler(NotAuthenticated, _not_authenticated)
    app.add_exception_handler(CsrfRejected, _csrf_rejected)
    app.add_exception_handler(IdentityError, _identity_error)
    app.add_exception_handler(SessionUnavailable, _session_unavailable)
