"""FastAPI dependencies: session cookie → session → principal → home access."""

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, Request

from smarthome.modules.identity.api.container import IdentityModule
from smarthome.modules.identity.application.services import HomeAccess
from smarthome.modules.identity.domain.model import HomeId, Permission
from smarthome.modules.identity.domain.principal import Principal
from smarthome.modules.identity.infrastructure.sessions import Session

# `__Host-` makes the browser enforce Secure, Path=/ and no Domain attribute, so a
# sibling subdomain can never set or overwrite these cookies.
SESSION_COOKIE = "__Host-smarthome_session"
LOGIN_COOKIE = "__Host-smarthome_login"
CSRF_HEADER = "x-csrf-token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class NotAuthenticated(Exception):
    pass


class CsrfRejected(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SessionContext:
    session_id: str
    session: Session


def identity_module(request: Request) -> IdentityModule:
    module: IdentityModule = request.app.state.identity
    return module


Identity = Annotated[IdentityModule, Depends(identity_module)]


async def optional_session(request: Request, module: Identity) -> SessionContext | None:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    session = await module.sessions.resolve(session_id)
    return SessionContext(session_id, session) if session else None


async def require_session(
    context: Annotated[SessionContext | None, Depends(optional_session)],
) -> SessionContext:
    if context is None:
        raise NotAuthenticated
    return context


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def csrf_protected_session(
    request: Request,
    module: Identity,
    context: Annotated[SessionContext, Depends(require_session)],
) -> SessionContext:
    """Unsafe methods need the session's CSRF token in a header and, when the browser
    sends one, an Origin we serve. SameSite=Strict already blocks most cross-site
    requests; this covers same-site attackers (other ports, subdomains) and old browsers.
    """
    if request.method in UNSAFE_METHODS:
        settings = module.settings
        allowed = {_origin(settings.web_app_url), _origin(settings.bff_public_url)}
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed:
            raise CsrfRejected("cross-origin request")
        token = request.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(token.encode(), context.session.csrf_token.encode()):
            raise CsrfRejected("missing or invalid CSRF token")
    return context


async def current_principal(
    context: Annotated[SessionContext, Depends(csrf_protected_session)],
) -> Principal:
    return context.session.principal()


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_home_access(permission: Permission) -> Callable[..., Awaitable[HomeAccess]]:
    """Dependency for `/homes/{home_id}/...` routes in any module."""

    async def dependency(
        home_id: UUID, principal: CurrentPrincipal, module: Identity
    ) -> HomeAccess:
        return await module.service.access(principal, HomeId(home_id), permission)

    return dependency
