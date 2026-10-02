"""Server-side sessions for the BFF.

The browser only ever holds an opaque, random session id in an HttpOnly cookie. Tokens
stay in Redis, keyed by sha256(session id) so a Redis dump does not hand out usable
cookies.
"""

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import structlog
from redis.asyncio import Redis

from smarthome.modules.identity.domain.model import UserId
from smarthome.modules.identity.domain.principal import Principal
from smarthome.modules.identity.infrastructure.oidc import (
    IdTokenClaims,
    OidcClient,
    Pkce,
    ProviderUnavailable,
    RefreshRejected,
    TokenSet,
)
from smarthome.shared.clock import Clock

log = structlog.get_logger(__name__)

SESSION_PREFIX = "bff:session:"
LOGIN_PREFIX = "bff:login:"
REFRESH_LOCK_PREFIX = "bff:refresh-lock:"
LOGIN_TTL = timedelta(minutes=10)
# Refresh a little before expiry so a request never starts with an about-to-die token.
REFRESH_SKEW = timedelta(seconds=30)
REFRESH_LOCK_TTL_MS = 10_000
REFRESH_WAIT_STEP = 0.05
REFRESH_WAIT_STEPS = 100

_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


class SessionUnavailable(Exception):
    """The session could not be validated right now (IdP down); fail closed."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Session:
    user_id: str
    email: str | None
    display_name: str | None
    authenticated_at: datetime
    created_at: datetime
    absolute_expires_at: datetime
    csrf_token: str
    access_token: str
    access_expires_at: datetime
    refresh_token: str | None
    refresh_expires_at: datetime | None
    id_token: str | None

    def principal(self) -> Principal:
        return Principal(
            user_id=UserId(self.user_id),
            email=self.email,
            display_name=self.display_name,
            authenticated_at=self.authenticated_at,
        )

    def needs_refresh(self, now: datetime) -> bool:
        return now >= self.access_expires_at - REFRESH_SKEW

    def with_tokens(self, tokens: TokenSet) -> "Session":
        return replace(
            self,
            access_token=tokens.access_token,
            access_expires_at=tokens.access_expires_at,
            # Rotation: the provider issues a new refresh token on every refresh.
            refresh_token=tokens.refresh_token or self.refresh_token,
            refresh_expires_at=tokens.refresh_expires_at or self.refresh_expires_at,
            id_token=tokens.id_token or self.id_token,
        )

    def expires_at(self) -> datetime:
        if self.refresh_expires_at is None:
            return self.absolute_expires_at
        return min(self.absolute_expires_at, self.refresh_expires_at)

    def to_json(self) -> str:
        data: dict[str, Any] = asdict(self)
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        return json.dumps(data)

    @staticmethod
    def from_json(raw: str) -> "Session":
        data: dict[str, Any] = json.loads(raw)
        for key in (
            "authenticated_at",
            "created_at",
            "absolute_expires_at",
            "access_expires_at",
            "refresh_expires_at",
        ):
            if data[key] is not None:
                data[key] = datetime.fromisoformat(data[key])
        return Session(**data)


@dataclass(frozen=True, slots=True)
class LoginTransaction:
    state: str
    nonce: str
    code_verifier: str
    return_to: str
    force_reauthentication: bool
    started_at: datetime


class RedisSessionStore:
    def __init__(self, redis: Redis, clock: Clock) -> None:
        self._redis = redis
        self._clock = clock

    async def save(self, session_id: str, session: Session) -> None:
        ttl = session.expires_at() - self._clock.now()
        if ttl <= timedelta(0):
            await self.delete(session_id)
            return
        await self._redis.set(
            SESSION_PREFIX + _digest(session_id),
            session.to_json(),
            px=int(ttl.total_seconds() * 1000),
        )

    async def get(self, session_id: str) -> Session | None:
        raw = await self._redis.get(SESSION_PREFIX + _digest(session_id))
        return Session.from_json(str(raw)) if raw else None

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(SESSION_PREFIX + _digest(session_id))

    async def put_login(self, tx: LoginTransaction) -> None:
        data = asdict(tx) | {"started_at": tx.started_at.isoformat()}
        await self._redis.set(
            LOGIN_PREFIX + _digest(tx.state),
            json.dumps(data),
            px=int(LOGIN_TTL.total_seconds() * 1000),
        )

    async def take_login(self, state: str) -> LoginTransaction | None:
        """Single use: a replayed callback finds nothing."""
        raw = await self._redis.getdel(LOGIN_PREFIX + _digest(state))
        if not raw:
            return None
        data = json.loads(str(raw))
        data["started_at"] = datetime.fromisoformat(data["started_at"])
        return LoginTransaction(**data)

    @asynccontextmanager
    async def refresh_lock(self, session_id: str) -> AsyncIterator[bool]:
        key = REFRESH_LOCK_PREFIX + _digest(session_id)
        token = secrets.token_hex(16)
        acquired = bool(await self._redis.set(key, token, nx=True, px=REFRESH_LOCK_TTL_MS))
        try:
            yield acquired
        finally:
            if acquired:
                await self._redis.eval(_RELEASE_LOCK, 1, key, token)


class LoginFailed(Exception):
    pass


class SessionManager:
    """BFF session lifecycle: login transaction → session → refresh → logout."""

    def __init__(
        self,
        *,
        oidc: OidcClient,
        store: RedisSessionStore,
        clock: Clock,
        absolute_lifetime: timedelta,
    ) -> None:
        self._oidc = oidc
        self._store = store
        self._clock = clock
        self._absolute_lifetime = absolute_lifetime

    async def begin_login(
        self, *, redirect_uri: str, return_to: str, force_reauthentication: bool
    ) -> tuple[str, str]:
        """Returns (authorization URL, state). The state also goes into a browser cookie."""
        pkce = Pkce.generate()
        tx = LoginTransaction(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=pkce.verifier,
            return_to=return_to,
            force_reauthentication=force_reauthentication,
            started_at=self._clock.now(),
        )
        await self._store.put_login(tx)
        url = await self._oidc.authorization_url(
            redirect_uri=redirect_uri,
            state=tx.state,
            nonce=tx.nonce,
            code_challenge=pkce.challenge,
            force_reauthentication=force_reauthentication,
        )
        return url, tx.state

    async def complete_login(
        self, *, state: str, browser_state: str | None, code: str, redirect_uri: str
    ) -> tuple[str, Session, str]:
        """Returns (new session id, session, return_to)."""
        # The state must come back to the same browser that started the login (login CSRF).
        if browser_state is None or not secrets.compare_digest(state, browser_state):
            raise LoginFailed("state does not match this browser's login attempt")
        tx = await self._store.take_login(state)
        if tx is None:
            raise LoginFailed("unknown or expired login attempt")

        tokens = await self._oidc.exchange_code(
            code=code, code_verifier=tx.code_verifier, redirect_uri=redirect_uri
        )
        if tokens.id_token is None:
            raise LoginFailed("provider returned no ID token")
        claims: IdTokenClaims = await self._oidc.validate_id_token(tokens.id_token, nonce=tx.nonce)
        if tx.force_reauthentication and claims.authenticated_at < tx.started_at - REFRESH_SKEW:
            raise LoginFailed("re-authentication was requested but did not happen")

        now = self._clock.now()
        session = Session(
            user_id=claims.subject,
            email=claims.email,
            display_name=claims.name,
            authenticated_at=claims.authenticated_at,
            created_at=now,
            absolute_expires_at=now + self._absolute_lifetime,
            csrf_token=secrets.token_urlsafe(32),
            access_token=tokens.access_token,
            access_expires_at=tokens.access_expires_at,
            refresh_token=tokens.refresh_token,
            refresh_expires_at=tokens.refresh_expires_at,
            id_token=tokens.id_token,
        )
        # Always a fresh id: a session id known before login (fixation) is worthless after.
        session_id = secrets.token_urlsafe(32)
        await self._store.save(session_id, session)
        return session_id, session, tx.return_to

    async def resolve(self, session_id: str) -> Session | None:
        session = await self._store.get(session_id)
        if session is None:
            return None
        now = self._clock.now()
        if now >= session.absolute_expires_at:
            await self._store.delete(session_id)
            return None
        if not session.needs_refresh(now):
            return session
        return await self._refresh(session_id, session)

    async def _refresh(self, session_id: str, stale: Session) -> Session | None:
        async with self._store.refresh_lock(session_id) as acquired:
            if acquired:
                # Re-read: another instance may have refreshed between our read and the lock.
                current = await self._store.get(session_id)
                if current is None or not current.needs_refresh(self._clock.now()):
                    return current
                if current.refresh_token is None:
                    await self._store.delete(session_id)
                    return None
                try:
                    tokens = await self._oidc.refresh(current.refresh_token)
                except RefreshRejected as exc:
                    # Rotated elsewhere, revoked, or the IdP session ended: the session is over.
                    log.warning(
                        "session_refresh_rejected", user_id=current.user_id, reason=str(exc)
                    )
                    await self._store.delete(session_id)
                    return None
                except ProviderUnavailable as exc:
                    raise SessionUnavailable(str(exc)) from exc
                refreshed = current.with_tokens(tokens)
                await self._store.save(session_id, refreshed)
                return refreshed

        # Someone else is refreshing this session: wait for their result instead of
        # spending the same refresh token twice (which rotation would treat as reuse).
        for _ in range(REFRESH_WAIT_STEPS):
            await asyncio.sleep(REFRESH_WAIT_STEP)
            current = await self._store.get(session_id)
            if current is None:
                return None
            if current.access_expires_at > stale.access_expires_at:
                return current
        raise SessionUnavailable("timed out waiting for a concurrent session refresh")

    async def logout(self, session_id: str, *, post_logout_redirect_uri: str) -> str:
        session = await self._store.get(session_id)
        await self._store.delete(session_id)
        return await self._oidc.end_session_url(
            id_token_hint=session.id_token if session else None,
            post_logout_redirect_uri=post_logout_redirect_uri,
        )
