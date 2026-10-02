"""BFF session refresh against real Redis, with a scripted identity provider."""

import asyncio
import secrets
from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest
from redis.asyncio import Redis

from smarthome.modules.identity.infrastructure.oidc import OidcClient, RefreshRejected, TokenSet
from smarthome.modules.identity.infrastructure.sessions import (
    RedisSessionStore,
    Session,
    SessionManager,
)
from smarthome.shared.clock import SystemClock

PARALLEL_REQUESTS = 10
IDP_LATENCY = 0.2


class ScriptedProvider:
    def __init__(self) -> None:
        self.refresh_calls: list[str] = []
        self.reject = False

    async def refresh(self, refresh_token: str) -> TokenSet:
        self.refresh_calls.append(refresh_token)
        await asyncio.sleep(IDP_LATENCY)
        if self.reject:
            raise RefreshRejected("Token is not active")
        now = SystemClock().now()
        return TokenSet(
            access_token=f"access-{len(self.refresh_calls)}",
            access_expires_at=now + timedelta(minutes=5),
            refresh_token=f"refresh-{len(self.refresh_calls)}",
            refresh_expires_at=now + timedelta(minutes=30),
            id_token=None,
        )


@pytest.fixture
def provider() -> ScriptedProvider:
    return ScriptedProvider()


@pytest.fixture
def store(redis: Redis) -> RedisSessionStore:
    return RedisSessionStore(redis, SystemClock())


@pytest.fixture
def manager(provider: ScriptedProvider, store: RedisSessionStore) -> SessionManager:
    return SessionManager(
        oidc=cast("OidcClient", provider),
        store=store,
        clock=SystemClock(),
        absolute_lifetime=timedelta(hours=12),
    )


async def expired_session(store: RedisSessionStore) -> str:
    now = SystemClock().now()
    session = Session(
        user_id="alice",
        email=None,
        display_name="Alice",
        authenticated_at=now - timedelta(hours=1),
        created_at=now - timedelta(hours=1),
        absolute_expires_at=now + timedelta(hours=11),
        csrf_token="csrf",
        access_token="access-0",
        access_expires_at=now - timedelta(seconds=1),
        refresh_token="refresh-0",
        refresh_expires_at=now + timedelta(minutes=20),
        id_token=None,
    )
    session_id = secrets.token_urlsafe(32)
    await store.save(session_id, session)
    return session_id


async def test_parallel_requests_on_an_expired_session_refresh_it_exactly_once(
    manager: SessionManager, store: RedisSessionStore, provider: ScriptedProvider
) -> None:
    session_id = await expired_session(store)

    results = await asyncio.gather(*(manager.resolve(session_id) for _ in range(PARALLEL_REQUESTS)))

    # Spending one refresh token twice would look like token theft to a rotating IdP.
    assert provider.refresh_calls == ["refresh-0"]
    assert {r.access_token for r in results if r} == {"access-1"}
    stored = await store.get(session_id)
    assert stored is not None
    assert stored.refresh_token == "refresh-1"


async def test_a_rejected_refresh_ends_the_session(
    manager: SessionManager, store: RedisSessionStore, provider: ScriptedProvider
) -> None:
    session_id = await expired_session(store)
    provider.reject = True

    assert await manager.resolve(session_id) is None
    assert await store.get(session_id) is None


async def test_a_session_past_its_absolute_lifetime_is_dropped_without_asking_the_idp(
    manager: SessionManager, store: RedisSessionStore, provider: ScriptedProvider
) -> None:
    session_id = await expired_session(store)
    session = await store.get(session_id)
    assert session is not None
    await store.save(
        session_id,
        replace(session, absolute_expires_at=SystemClock().now() + timedelta(milliseconds=50)),
    )
    await asyncio.sleep(0.1)

    assert await manager.resolve(session_id) is None
    assert provider.refresh_calls == []


async def test_session_ids_are_not_stored_in_redis_in_plaintext(
    store: RedisSessionStore, redis: Redis
) -> None:
    session_id = await expired_session(store)

    keys = [k async for k in redis.scan_iter("bff:session:*")]
    assert keys
    assert all(session_id not in k for k in keys)
