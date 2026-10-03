from dataclasses import dataclass
from datetime import datetime

from smarthome.modules.identity.application.ports import UnitOfWork, UnitOfWorkFactory
from smarthome.modules.identity.domain.errors import (
    AccessDenied,
    AlreadyMember,
    HomeNotFound,
    InvalidInvitation,
    LastOwner,
)
from smarthome.modules.identity.domain.model import (
    Home,
    HomeId,
    Invitation,
    Membership,
    Permission,
    Role,
    UserId,
)
from smarthome.modules.identity.domain.principal import Principal
from smarthome.shared.audit import AuditEntry, AuditEvent
from smarthome.shared.clock import Clock


@dataclass(frozen=True, slots=True)
class HomeAccess:
    home: Home
    membership: Membership

    @property
    def role(self) -> Role:
        return self.membership.role

    def allows(
        self, permission: Permission, *, now: datetime, device_id: str | None = None
    ) -> bool:
        return self.membership.allows(permission, now=now, device_id=device_id)


@dataclass(frozen=True, slots=True)
class MemberView:
    membership: Membership
    display_name: str | None


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: UserId
    role: Role
    email: str | None


@dataclass(frozen=True, slots=True)
class Audience:
    """Who hears about what happens in a home, and the home's clock."""

    home_id: HomeId
    timezone: str
    recipients: tuple[Recipient, ...]


@dataclass(frozen=True, slots=True)
class AuditView:
    entries: list[AuditEntry]
    chain_intact: bool


class IdentityService:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def record_login(self, principal: Principal) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            await uow.users.record_login(
                principal.user_id,
                email=principal.email,
                display_name=principal.display_name,
                now=now,
            )
            await uow.commit()

    async def create_home(self, principal: Principal, *, name: str, timezone: str) -> Home:
        now = self._clock.now()
        home = Home.create(name=name, timezone=timezone, created_by=principal.user_id, now=now)
        owner = Membership.grant(
            home_id=home.id,
            user_id=principal.user_id,
            role=Role.OWNER,
            granted_by=principal.user_id,
            now=now,
        )
        async with self._uow() as uow:
            await uow.homes.add(home)
            await uow.memberships.add(owner)
            await uow.audit.append(
                AuditEvent(
                    actor=principal.user_id,
                    action="home.created",
                    target_type="home",
                    target_id=str(home.id),
                    tenant_id=home.id,
                    details={"name": home.name},
                ),
                occurred_at=now,
            )
            await uow.commit()
        return home

    async def my_homes(self, principal: Principal) -> list[HomeAccess]:
        now = self._clock.now()
        async with self._uow() as uow:
            rows = await uow.memberships.current_for_user(principal.user_id)
        return [HomeAccess(home, m) for home, m in rows if m.is_active(now)]

    async def access(
        self,
        principal: Principal,
        home_id: HomeId,
        permission: Permission,
        *,
        device_id: str | None = None,
    ) -> HomeAccess:
        """Resolve the caller's access to a home, or raise.

        Non-members (and expired or revoked members) get `HomeNotFound`, the same answer
        as for a home that does not exist, so ids cannot be probed.
        """
        async with self._uow() as uow:
            return await self._access(uow, principal, home_id, permission, device_id=device_id)

    async def members(self, principal: Principal, home_id: HomeId) -> list[MemberView]:
        now = self._clock.now()
        async with self._uow() as uow:
            await self._access(uow, principal, home_id, Permission.VIEW_HOME)
            memberships = [
                m for m in await uow.memberships.current_for_home(home_id) if m.is_active(now)
            ]
            names = await uow.users.display_names([m.user_id for m in memberships])
        return [MemberView(m, names.get(m.user_id)) for m in memberships]

    async def audience(self, home_id: HomeId) -> Audience | None:
        """Active members other than guests (a guest pass is for using some devices, not
        for being told about the house). None if the home does not exist."""
        now = self._clock.now()
        async with self._uow() as uow:
            home = await uow.homes.get(home_id)
            if home is None:
                return None
            members = [
                m
                for m in await uow.memberships.current_for_home(home_id)
                if m.is_active(now) and m.role is not Role.GUEST
            ]
            emails = await uow.users.emails([m.user_id for m in members])
        return Audience(
            home_id,
            home.timezone,
            tuple(Recipient(m.user_id, m.role, emails.get(m.user_id)) for m in members),
        )

    async def invite(
        self,
        principal: Principal,
        home_id: HomeId,
        *,
        role: Role,
        guest_access_expires_at: datetime | None = None,
        device_scope: frozenset[str] | None = None,
    ) -> tuple[Invitation, str]:
        now = self._clock.now()
        async with self._uow() as uow:
            await self._access(uow, principal, home_id, Permission.MANAGE_MEMBERS)
            invitation, token = Invitation.issue(
                home_id=home_id,
                role=role,
                invited_by=principal.user_id,
                now=now,
                guest_access_expires_at=guest_access_expires_at,
                device_scope=device_scope,
            )
            await uow.invitations.add(invitation)
            await uow.audit.append(
                AuditEvent(
                    actor=principal.user_id,
                    action="invitation.created",
                    target_type="invitation",
                    target_id=str(invitation.id),
                    tenant_id=home_id,
                    details={
                        "role": role.value,
                        "guest_access_expires_at": guest_access_expires_at,
                        "device_scope": sorted(device_scope) if device_scope else None,
                    },
                ),
                occurred_at=now,
            )
            await uow.commit()
        return invitation, token

    async def accept_invitation(self, principal: Principal, token: str) -> HomeAccess:
        now = self._clock.now()
        async with self._uow() as uow:
            invitation = await uow.invitations.lock_by_token_hash(Invitation.hash_token(token))
            if invitation is None:
                raise InvalidInvitation("invitation is expired, revoked or already used")
            existing = await uow.memberships.current(invitation.home_id, principal.user_id)
            if existing is not None and existing.is_active(now):
                raise AlreadyMember("you are already a member of this home")
            if existing is not None:
                # An expired guest pass being replaced by a new invitation.
                await uow.memberships.save_revocation(existing.close_expired(now=now))
            accepted, membership = invitation.accept(by=principal.user_id, now=now)
            await uow.invitations.save_acceptance(accepted)
            await uow.memberships.add(membership)
            home = await uow.homes.get(invitation.home_id)
            if home is None:  # pragma: no cover - FK guarantees it
                raise HomeNotFound(str(invitation.home_id))
            await uow.audit.append(
                AuditEvent(
                    actor=principal.user_id,
                    action="invitation.accepted",
                    target_type="invitation",
                    target_id=str(invitation.id),
                    tenant_id=invitation.home_id,
                    details={"role": membership.role.value, "membership_id": str(membership.id)},
                ),
                occurred_at=now,
            )
            await uow.commit()
        return HomeAccess(home, membership)

    async def revoke_member(self, principal: Principal, home_id: HomeId, user_id: UserId) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            leaving_voluntarily = user_id == principal.user_id
            await self._access(
                uow,
                principal,
                home_id,
                Permission.VIEW_HOME if leaving_voluntarily else Permission.MANAGE_MEMBERS,
            )
            # Lock every live membership of the home: two owners removing each other at
            # the same time must not both succeed and leave the home without an owner.
            current = await uow.memberships.lock_current_for_home(home_id)
            target = next((m for m in current if m.user_id == user_id and m.is_active(now)), None)
            if target is None:
                raise HomeNotFound(f"no active member {user_id} in this home")
            if target.role is Role.OWNER:
                owners = [m for m in current if m.role is Role.OWNER and m.is_active(now)]
                if len(owners) <= 1:
                    raise LastOwner("transfer ownership before removing the last owner")
            await uow.memberships.save_revocation(target.revoke(by=principal.user_id, now=now))
            await uow.audit.append(
                AuditEvent(
                    actor=principal.user_id,
                    action="membership.revoked",
                    target_type="membership",
                    target_id=str(target.id),
                    tenant_id=home_id,
                    details={"user_id": user_id, "role": target.role.value},
                ),
                occurred_at=now,
            )
            await uow.commit()

    async def audit_log(self, principal: Principal, home_id: HomeId, *, limit: int) -> AuditView:
        async with self._uow() as uow:
            await self._access(uow, principal, home_id, Permission.VIEW_AUDIT_LOG)
            entries = await uow.audit.recent(home_id, limit=limit)
            breaks = await uow.audit.verify(home_id)
        return AuditView(entries=entries, chain_intact=not breaks)

    async def _access(
        self,
        uow: UnitOfWork,
        principal: Principal,
        home_id: HomeId,
        permission: Permission,
        *,
        device_id: str | None = None,
    ) -> HomeAccess:
        now = self._clock.now()
        membership = await uow.memberships.current(home_id, principal.user_id)
        if membership is None or not membership.is_active(now):
            raise HomeNotFound(str(home_id))
        if not membership.allows(permission, now=now, device_id=device_id):
            raise AccessDenied(f"{membership.role.value} cannot {permission.value}")
        home = await uow.homes.get(home_id)
        if home is None:  # pragma: no cover - FK guarantees it
            raise HomeNotFound(str(home_id))
        return HomeAccess(home, membership)
