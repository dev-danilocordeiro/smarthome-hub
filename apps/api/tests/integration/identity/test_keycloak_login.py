"""End-to-end sign-in against a real Keycloak running the project's realm export."""

import re

import httpx
import pytest
from redis.asyncio import Redis

from smarthome.modules.identity.api.dependencies import LOGIN_COOKIE, SESSION_COOKIE
from smarthome.modules.identity.infrastructure.oidc import OidcClient, RefreshRejected
from smarthome.modules.identity.infrastructure.sessions import RedisSessionStore
from smarthome.shared.clock import SystemClock
from smarthome.shared.config import Settings
from tests.integration.identity.browser import Browser
from tests.integration.identity.conftest import APP_URL, WEB_URL

JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


async def test_signing_in_yields_a_strict_httponly_session_and_no_token_ever_reaches_the_browser(
    browser: Browser,
) -> None:
    landing = await browser.login(query="?return_to=/homes")

    assert landing == f"{WEB_URL}/homes"
    session_cookie = next(c for c in browser.raw_set_cookies if c.startswith(SESSION_COOKIE))
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in session_cookie.lower()
    assert "max-age" not in session_cookie.lower()  # browser-session cookie

    me = (await browser.request("GET", f"{APP_URL}/auth/session")).json()
    assert me["user"]["email"] == "alice@smarthome.local"
    assert me["user"]["name"] == "Alice Owner"

    app_traffic = [b for b in browser.bodies if b and "kc-form" not in b]
    assert not any(JWT.search(b) for b in app_traffic)
    assert not any(JWT.search(v) for v in browser.cookies.get("api", {}).values())


async def test_a_signed_in_user_creates_a_home_and_the_csrf_token_is_enforced(
    browser: Browser,
) -> None:
    await browser.login()
    csrf = (await browser.request("GET", f"{APP_URL}/auth/session")).json()["csrf_token"]

    blocked = await browser.request("POST", f"{APP_URL}/homes", json={"name": "Casa"})
    created = await browser.request(
        "POST", f"{APP_URL}/homes", json={"name": "Casa"}, headers={"x-csrf-token": csrf}
    )

    assert blocked.status_code == 403
    assert created.status_code == 201
    assert created.json()["role"] == "owner"


async def test_a_callback_delivered_to_a_different_browser_does_not_sign_it_in(
    browser: Browser, app_client: httpx.AsyncClient
) -> None:
    page = await browser.start_login()
    callback = await browser.submit_credentials(page, "alice")

    victim = Browser(app=app_client, network=httpx.AsyncClient(), app_url=APP_URL)
    landing = await victim.finish(callback)

    assert "login_error" in landing
    assert victim.cookie(SESSION_COOKIE) is None


async def test_a_replayed_callback_is_refused(browser: Browser) -> None:
    page = await browser.start_login()
    callback = await browser.submit_credentials(page, "alice")
    login_state = browser.cookie(LOGIN_COOKIE)
    await browser.finish(callback)

    browser.cookies.setdefault("api", {})[LOGIN_COOKIE] = login_state or ""
    replay = await browser.finish(callback)

    assert "login_error" in replay


async def test_reauthentication_shows_the_login_form_even_with_a_live_sso_session(
    browser: Browser,
) -> None:
    await browser.login()

    silent = await browser.request("GET", f"{APP_URL}/auth/login")
    sso = await browser.request("GET", silent.headers["location"])
    forced = await browser.start_login("?reauth=true")

    assert sso.status_code == 302  # Keycloak signs the user in without asking
    assert sso.headers["location"].startswith(f"{APP_URL}/auth/callback")
    assert 'id="kc-form-login"' in forced.text


async def test_rotated_refresh_tokens_cannot_be_reused(
    browser: Browser, oidc_settings: Settings, redis: Redis
) -> None:
    await browser.login()
    session_id = browser.cookie(SESSION_COOKIE)
    assert session_id
    session = await RedisSessionStore(redis, SystemClock()).get(session_id)
    assert session is not None
    assert session.refresh_token is not None

    async with httpx.AsyncClient() as http:
        oidc = OidcClient(
            http=http,
            discovery_url=oidc_settings.oidc_discovery_url,
            client_id=oidc_settings.oidc_client_id,
            client_secret=oidc_settings.oidc_client_secret.get_secret_value(),
            clock=SystemClock(),
        )
        rotated = await oidc.refresh(session.refresh_token)
        assert rotated.refresh_token not in (None, session.refresh_token)

        with pytest.raises(RefreshRejected):
            await oidc.refresh(session.refresh_token)


async def test_logging_out_ends_the_session_and_points_the_browser_at_keycloak_logout(
    browser: Browser, keycloak_url: str
) -> None:
    await browser.login()
    csrf = (await browser.request("GET", f"{APP_URL}/auth/session")).json()["csrf_token"]

    out = await browser.request("POST", f"{APP_URL}/auth/logout", headers={"x-csrf-token": csrf})
    after = await browser.request("GET", f"{APP_URL}/auth/session")

    assert out.json()["logout_url"].startswith(f"{keycloak_url}/realms/smarthome/")
    assert "id_token_hint=" in out.json()["logout_url"]
    assert after.status_code == 401
