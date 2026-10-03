"""In-memory adapters for the identity ports, for fast service tests."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

from smarthome.modules.identity.domain.model import Home, HomeId, Invitation, Membership, UserId
from smarthome.shared.audit import AuditEntry, AuditEvent, ChainBreak


class FakeClock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


@dataclass
class Store:
    homes: dict[HomeId, Home] = field(default_factory=dict)
    memberships: list[Membership] = field(default_factory=list)
    invitations: dict[bytes, Invitation] = field(default_factory=dict)
    names: dict[UserId, str] = field(default_factory=dict)
    emails: dict[UserId, str] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)
    commits: int = 0


class _Homes:
    def __init__(self, s: Store) -> None:
        self.s = s

    async def add(self, home: Home) -> None:
        self.s.homes[home.id] = home

    async def get(self, home_id: HomeId) -> Home | None:
        return self.s.homes.get(home_id)


class _Memberships:
    def __init__(self, s: Store) -> None:
        self.s = s

    async def add(self, membership: Membership) -> None:
        if await self.current(membership.home_id, membership.user_id):
            raise AssertionError("unique index violation: active membership exists")
        self.s.memberships.append(membership)

    async def current(self, home_id: HomeId, user_id: UserId) -> Membership | None:
        return next(
            (
                m
                for m in self.s.memberships
                if m.home_id == home_id and m.user_id == user_id and m.revoked_at is None
            ),
            None,
        )

    async def current_for_user(self, user_id: UserId) -> list[tuple[Home, Membership]]:
        return [
            (self.s.homes[m.home_id], m)
            for m in self.s.memberships
            if m.user_id == user_id and m.revoked_at is None
        ]

    async def current_for_home(self, home_id: HomeId) -> list[Membership]:
        return [m for m in self.s.memberships if m.home_id == home_id and m.revoked_at is None]

    async def lock_current_for_home(self, home_id: HomeId) -> list[Membership]:
        return await self.current_for_home(home_id)

    async def save_revocation(self, membership: Membership) -> None:
        self.s.memberships = [
            membership if m.id == membership.id else m for m in self.s.memberships
        ]


class _Invitations:
    def __init__(self, s: Store) -> None:
        self.s = s

    async def add(self, invitation: Invitation) -> None:
        self.s.invitations[invitation.token_hash] = invitation

    async def lock_by_token_hash(self, token_hash: bytes) -> Invitation | None:
        return self.s.invitations.get(token_hash)

    async def save_acceptance(self, invitation: Invitation) -> None:
        self.s.invitations[invitation.token_hash] = invitation


class _Users:
    def __init__(self, s: Store) -> None:
        self.s = s

    async def record_login(
        self, user_id: UserId, *, email: str | None, display_name: str | None, now: datetime
    ) -> None:
        self.s.names[user_id] = display_name or email or user_id
        if email:
            self.s.emails[user_id] = email

    async def display_names(self, user_ids: list[UserId]) -> dict[UserId, str]:
        return {u: self.s.names[u] for u in user_ids if u in self.s.names}

    async def emails(self, user_ids: list[UserId]) -> dict[UserId, str]:
        return {u: self.s.emails[u] for u in user_ids if u in self.s.emails}


class _Audit:
    def __init__(self, s: Store) -> None:
        self.s = s

    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None:
        self.s.audit.append(event)

    async def recent(self, tenant_id: HomeId, *, limit: int) -> list[AuditEntry]:
        return []

    async def verify(self, tenant_id: HomeId) -> list[ChainBreak]:
        return []


class FakeUnitOfWork:
    """Stages writes on a copy of the store and publishes them only on commit."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def __aenter__(self) -> Self:
        self._work = replace(
            self._store,
            homes=dict(self._store.homes),
            memberships=list(self._store.memberships),
            invitations=dict(self._store.invitations),
            names=dict(self._store.names),
            audit=list(self._store.audit),
        )
        self.homes = _Homes(self._work)
        self.memberships = _Memberships(self._work)
        self.invitations = _Invitations(self._work)
        self.users = _Users(self._work)
        self.audit = _Audit(self._work)
        return self

    async def commit(self) -> None:
        s = self._store
        s.homes, s.memberships = self._work.homes, self._work.memberships
        s.invitations, s.names, s.audit = self._work.invitations, self._work.names, self._work.audit
        s.commits += 1

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None
