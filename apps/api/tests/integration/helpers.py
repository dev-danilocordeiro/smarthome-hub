"""Helpers shared by integration tests: signed-in members and waiting on other tasks."""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis

from smarthome.modules.identity.api.dependencies import SESSION_COOKIE
from smarthome.modules.identity.domain.model import UserId
from smarthome.modules.identity.domain.principal import Principal
from smarthome.modules.identity.infrastructure.sessions import RedisSessionStore, Session
from smarthome.shared.clock import SystemClock


async def eventually(check: Callable[[], Awaitable[bool]], within_s: float = 10.0) -> None:
    async with asyncio.timeout(within_s):
        while not await check():  # noqa: ASYNC110 - state owned by other tasks
            await asyncio.sleep(0.1)


@dataclass(frozen=True)
class Member:
    principal: Principal
    headers: dict[str, str]


async def sign_in(
    redis: Redis, user_id: str, *, authenticated_ago: timedelta = timedelta(seconds=10)
) -> Member:
    """A BFF session as the login callback would leave it, without the IdP round trip."""
    now = SystemClock().now()
    principal = Principal(
        user_id=UserId(user_id),
        email=None,
        display_name=user_id.title(),
        authenticated_at=now - authenticated_ago,
    )
    session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    session = Session(
        user_id=user_id,
        email=None,
        display_name=principal.display_name,
        authenticated_at=principal.authenticated_at,
        created_at=now,
        absolute_expires_at=now + timedelta(hours=1),
        csrf_token=csrf,
        access_token="unused",
        access_expires_at=now + timedelta(hours=1),
        refresh_token=None,
        refresh_expires_at=None,
        id_token=None,
    )
    await RedisSessionStore(redis, SystemClock()).save(session_id, session)
    return Member(principal, {"cookie": f"{SESSION_COOKIE}={session_id}", "x-csrf-token": csrf})
