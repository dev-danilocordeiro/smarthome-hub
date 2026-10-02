"""Postgres adapters for the identity ports (schema `identity`), using plain SQL.

Rows are mapped to domain objects explicitly; the domain never sees SQLAlchemy.
"""

from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from smarthome.modules.identity.domain.model import (
    Home,
    HomeId,
    Invitation,
    Membership,
    Role,
    UserId,
)
from smarthome.shared.audit import AuditEntry, AuditEvent, ChainBreak
from smarthome.shared.infrastructure.audit import PostgresAuditLog, read_chain, verify_tenant_chain

SELECT_HOME = text(
    "SELECT h.id AS h_id, h.name, h.timezone, h.created_by, h.created_at"
    " FROM identity.homes h WHERE h.id = :id"
)
SELECT_CURRENT_MEMBERSHIP = text(
    "SELECT m.id, m.home_id, m.user_id, m.role, m.granted_by, m.granted_at, m.expires_at,"
    " m.device_scope, m.revoked_at, m.revoked_by"
    " FROM identity.memberships m"
    " WHERE m.home_id = :home AND m.user_id = :user AND m.revoked_at IS NULL"
)
SELECT_CURRENT_FOR_USER = text(
    "SELECT m.id, m.home_id, m.user_id, m.role, m.granted_by, m.granted_at, m.expires_at,"
    " m.device_scope, m.revoked_at, m.revoked_by,"
    " h.id AS h_id, h.name, h.timezone, h.created_by, h.created_at"
    " FROM identity.memberships m JOIN identity.homes h ON h.id = m.home_id"
    " WHERE m.user_id = :user AND m.revoked_at IS NULL ORDER BY h.name"
)
# ORDER BY id gives every locker the same lock order, so concurrent revocations queue
# instead of deadlocking.
SELECT_CURRENT_FOR_HOME = text(
    "SELECT m.id, m.home_id, m.user_id, m.role, m.granted_by, m.granted_at, m.expires_at,"
    " m.device_scope, m.revoked_at, m.revoked_by"
    " FROM identity.memberships m"
    " WHERE m.home_id = :home AND m.revoked_at IS NULL ORDER BY m.id"
)
LOCK_CURRENT_FOR_HOME = text(
    "SELECT m.id, m.home_id, m.user_id, m.role, m.granted_by, m.granted_at, m.expires_at,"
    " m.device_scope, m.revoked_at, m.revoked_by"
    " FROM identity.memberships m"
    " WHERE m.home_id = :home AND m.revoked_at IS NULL ORDER BY m.id FOR UPDATE"
)


def _scope(value: list[str] | None) -> frozenset[str] | None:
    return frozenset(value) if value is not None else None


def _membership(row: Row[Any]) -> Membership:
    return Membership(
        id=row.id,
        home_id=HomeId(row.home_id),
        user_id=UserId(row.user_id),
        role=Role(row.role),
        granted_by=UserId(row.granted_by),
        granted_at=row.granted_at,
        expires_at=row.expires_at,
        device_scope=_scope(row.device_scope),
        revoked_at=row.revoked_at,
        revoked_by=UserId(row.revoked_by) if row.revoked_by else None,
    )


def _home(row: Row[Any]) -> Home:
    return Home(
        id=HomeId(row.h_id),
        name=row.name,
        timezone=row.timezone,
        created_by=UserId(row.created_by),
        created_at=row.created_at,
    )


class PostgresHomes:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, home: Home) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO identity.homes (id, name, timezone, created_by, created_at)"
                " VALUES (:id, :name, :tz, :by, :at)"
            ),
            {
                "id": home.id,
                "name": home.name,
                "tz": home.timezone,
                "by": home.created_by,
                "at": home.created_at,
            },
        )

    async def get(self, home_id: HomeId) -> Home | None:
        row = (
            await self._conn.execute(
                SELECT_HOME,
                {"id": home_id},
            )
        ).first()
        return _home(row) if row else None


class PostgresMemberships:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, membership: Membership) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO identity.memberships (id, home_id, user_id, role, granted_by,"
                " granted_at, expires_at, device_scope)"
                " VALUES (:id, :home, :user, :role, :by, :at, :expires, :scope)"
            ),
            {
                "id": membership.id,
                "home": membership.home_id,
                "user": membership.user_id,
                "role": membership.role.value,
                "by": membership.granted_by,
                "at": membership.granted_at,
                "expires": membership.expires_at,
                "scope": sorted(membership.device_scope) if membership.device_scope else None,
            },
        )

    async def current(self, home_id: HomeId, user_id: UserId) -> Membership | None:
        row = (
            await self._conn.execute(
                SELECT_CURRENT_MEMBERSHIP,
                {"home": home_id, "user": user_id},
            )
        ).first()
        return _membership(row) if row else None

    async def current_for_user(self, user_id: UserId) -> list[tuple[Home, Membership]]:
        rows = await self._conn.execute(
            SELECT_CURRENT_FOR_USER,
            {"user": user_id},
        )
        return [(_home(r), _membership(r)) for r in rows]

    async def current_for_home(self, home_id: HomeId) -> list[Membership]:
        return await self._for_home(home_id, lock=False)

    async def lock_current_for_home(self, home_id: HomeId) -> list[Membership]:
        return await self._for_home(home_id, lock=True)

    async def _for_home(self, home_id: HomeId, *, lock: bool) -> list[Membership]:
        query = LOCK_CURRENT_FOR_HOME if lock else SELECT_CURRENT_FOR_HOME
        rows = await self._conn.execute(query, {"home": home_id})
        return [_membership(r) for r in rows]

    async def save_revocation(self, membership: Membership) -> None:
        result = await self._conn.execute(
            text(
                "UPDATE identity.memberships SET revoked_at = :at, revoked_by = :by"
                " WHERE id = :id AND revoked_at IS NULL"
            ),
            {"id": membership.id, "at": membership.revoked_at, "by": membership.revoked_by},
        )
        if result.rowcount != 1:
            raise RuntimeError(f"membership {membership.id} was revoked concurrently")


class PostgresInvitations:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, invitation: Invitation) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO identity.invitations (id, home_id, token_hash, role, invited_by,"
                " created_at, expires_at, guest_access_expires_at, device_scope)"
                " VALUES (:id, :home, :hash, :role, :by, :at, :expires, :guest_expires, :scope)"
            ),
            {
                "id": invitation.id,
                "home": invitation.home_id,
                "hash": invitation.token_hash,
                "role": invitation.role.value,
                "by": invitation.invited_by,
                "at": invitation.created_at,
                "expires": invitation.expires_at,
                "guest_expires": invitation.guest_access_expires_at,
                "scope": sorted(invitation.device_scope) if invitation.device_scope else None,
            },
        )

    async def lock_by_token_hash(self, token_hash: bytes) -> Invitation | None:
        row = (
            await self._conn.execute(
                text(
                    "SELECT id, home_id, token_hash, role, invited_by, created_at, expires_at,"
                    " guest_access_expires_at, device_scope, accepted_by, accepted_at, revoked_at"
                    " FROM identity.invitations WHERE token_hash = :hash FOR UPDATE"
                ),
                {"hash": token_hash},
            )
        ).first()
        if row is None:
            return None
        return Invitation(
            id=row.id,
            home_id=HomeId(row.home_id),
            role=Role(row.role),
            token_hash=bytes(row.token_hash),
            invited_by=UserId(row.invited_by),
            created_at=row.created_at,
            expires_at=row.expires_at,
            guest_access_expires_at=row.guest_access_expires_at,
            device_scope=_scope(row.device_scope),
            accepted_by=UserId(row.accepted_by) if row.accepted_by else None,
            accepted_at=row.accepted_at,
            revoked_at=row.revoked_at,
        )

    async def save_acceptance(self, invitation: Invitation) -> None:
        await self._conn.execute(
            text(
                "UPDATE identity.invitations SET accepted_by = :by, accepted_at = :at"
                " WHERE id = :id AND accepted_at IS NULL"
            ),
            {"id": invitation.id, "by": invitation.accepted_by, "at": invitation.accepted_at},
        )


class PostgresUserDirectory:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def record_login(
        self, user_id: UserId, *, email: str | None, display_name: str | None, now: datetime
    ) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO identity.users (subject, email, display_name, first_seen_at,"
                " last_login_at) VALUES (:sub, :email, :name, :now, :now)"
                " ON CONFLICT (subject) DO UPDATE SET email = EXCLUDED.email,"
                " display_name = EXCLUDED.display_name, last_login_at = EXCLUDED.last_login_at"
            ),
            {"sub": user_id, "email": email, "name": display_name, "now": now},
        )

    async def display_names(self, user_ids: list[UserId]) -> dict[UserId, str]:
        if not user_ids:
            return {}
        rows = await self._conn.execute(
            text(
                "SELECT subject, coalesce(display_name, email, subject) AS name"
                " FROM identity.users WHERE subject = ANY(:ids)"
            ),
            {"ids": list(user_ids)},
        )
        return {UserId(r.subject): r.name for r in rows}


class PostgresAuditTrail:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn
        self._writer = PostgresAuditLog(conn)

    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None:
        await self._writer.append(event, occurred_at=occurred_at)

    async def recent(self, tenant_id: HomeId, *, limit: int) -> list[AuditEntry]:
        return await read_chain(self._conn, UUID(str(tenant_id)), limit=limit)

    async def verify(self, tenant_id: HomeId) -> list[ChainBreak]:
        return await verify_tenant_chain(self._conn, UUID(str(tenant_id)))


class PostgresUnitOfWork:
    """One connection, one transaction. Rolled back unless `commit()` was called."""

    homes: PostgresHomes
    memberships: PostgresMemberships
    invitations: PostgresInvitations
    users: PostgresUserDirectory
    audit: PostgresAuditTrail

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn: AsyncConnection | None = None
        self._tx: AsyncTransaction | None = None

    async def __aenter__(self) -> Self:
        self._conn = await self._engine.connect()
        self._tx = await self._conn.begin()
        self.homes = PostgresHomes(self._conn)
        self.memberships = PostgresMemberships(self._conn)
        self.invitations = PostgresInvitations(self._conn)
        self.users = PostgresUserDirectory(self._conn)
        self.audit = PostgresAuditTrail(self._conn)
        return self

    async def commit(self) -> None:
        if self._tx is None:
            raise RuntimeError("unit of work is not active")
        await self._tx.commit()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._tx is not None and self._tx.is_active:
                await self._tx.rollback()
        finally:
            if self._conn is not None:
                await self._conn.close()
            self._conn = None
            self._tx = None
