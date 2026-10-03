"""Backend-for-frontend authentication endpoints.

The browser never sees an OAuth token: it gets an opaque HttpOnly session cookie, and
the tokens stay server-side (see ADR 0003).
"""

import html
from typing import Annotated
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from smarthome.modules.identity.api.dependencies import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    Identity,
    NotAuthenticated,
    SessionContext,
    csrf_protected_session,
    optional_session,
)
from smarthome.modules.identity.api.schemas import LogoutOut, SessionOut, UserOut
from smarthome.modules.identity.infrastructure.oidc import OidcError
from smarthome.modules.identity.infrastructure.sessions import LoginFailed
from smarthome.shared.config import Settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_RETURN_TO = 512


def callback_uri(settings: Settings) -> str:
    return f"{settings.bff_public_url}/auth/callback"


def safe_return_to(value: str) -> str:
    """Only same-site relative paths: anything else would be an open redirect."""
    if (
        not value.startswith("/")
        or value.startswith(("//", "/\\"))
        or len(value) > MAX_RETURN_TO
        or not value.isprintable()
        or "\\" in value
    ):
        return "/"
    return value


def _navigate(url: str) -> HTMLResponse:
    """Finish the login with a same-origin navigation instead of a 302.

    The callback is reached through a redirect chain that started at the identity
    provider. Browsers withhold SameSite=Strict cookies on such cross-site chains, so a
    302 to the app would land without the session cookie that was just set. A page that
    navigates by itself starts a new, same-site navigation that carries the cookie.
    """
    target = html.escape(url, quote=True)
    body = (
        "<!doctype html><meta charset=utf-8>"
        f'<meta http-equiv="refresh" content="0;url={target}">'
        f'<title>Signing in…</title><a href="{target}">Continue</a>'
    )
    return HTMLResponse(body)


@router.get("/login", status_code=status.HTTP_302_FOUND)
async def login(
    module: Identity,
    return_to: Annotated[str, Query(max_length=MAX_RETURN_TO * 2)] = "/",
    reauth: bool = False,
) -> RedirectResponse:
    """Start the authorization code + PKCE flow. `reauth=true` forces a fresh login
    (used before critical actions such as unlocking a door)."""
    settings = module.settings
    url, state = await module.sessions.begin_login(
        redirect_uri=callback_uri(settings),
        return_to=safe_return_to(return_to),
        force_reauthentication=reauth,
    )
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    # Lax, not Strict: it has to survive the top-level redirect back from the IdP.
    response.set_cookie(
        LOGIN_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/callback", response_class=HTMLResponse, include_in_schema=False)
async def callback(
    request: Request,
    module: Identity,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    settings = module.settings
    if error or not code or not state:
        log.warning("login_failed", reason=error or "missing code or state")
        return _failed(settings, error or "invalid_request")
    try:
        session_id, session, return_to = await module.sessions.complete_login(
            state=state,
            browser_state=request.cookies.get(LOGIN_COOKIE),
            code=code,
            redirect_uri=callback_uri(settings),
        )
    except (LoginFailed, OidcError) as exc:
        log.warning("login_failed", reason=str(exc), error_type=type(exc).__name__)
        return _failed(settings, "login_failed")

    await module.service.record_login(session.principal())
    log.info("login_succeeded", user_id=session.user_id)
    response = _navigate(f"{settings.web_app_url}{return_to}")
    # No Max-Age: a browser-session cookie. The server-side session enforces lifetimes.
    response.set_cookie(
        SESSION_COOKIE, session_id, httponly=True, secure=True, samesite="strict", path="/"
    )
    response.delete_cookie(LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


def _failed(settings: Settings, reason: str) -> HTMLResponse:
    response = _navigate(f"{settings.web_app_url}/?{urlencode({'login_error': reason})}")
    response.delete_cookie(LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.get("/session", responses={401: {"description": "Not signed in"}})
async def current_session(
    context: Annotated[SessionContext | None, Depends(optional_session)],
) -> SessionOut:
    """Who is signed in, plus the CSRF token the SPA must echo on unsafe requests."""
    if context is None:
        raise NotAuthenticated
    s = context.session
    return SessionOut(
        user=UserOut(id=s.user_id, email=s.email, name=s.display_name),
        authenticated_at=s.authenticated_at,
        csrf_token=s.csrf_token,
    )


@router.post("/logout", response_model=LogoutOut)
async def logout(
    module: Identity,
    context: Annotated[SessionContext, Depends(csrf_protected_session)],
) -> JSONResponse:
    """End the BFF session and return the IdP logout URL for the SPA to navigate to,
    which also ends the single sign-on session at Keycloak."""
    url = await module.sessions.logout(
        context.session_id, post_logout_redirect_uri=f"{module.settings.web_app_url}/"
    )
    log.info("logout", user_id=context.session.user_id)
    response = JSONResponse(LogoutOut(logout_url=url).model_dump())
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response
