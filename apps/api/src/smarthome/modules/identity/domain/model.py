"""Homes (tenants), who belongs to them, and what each role may do.

Users themselves live in the identity provider (Keycloak); here a user is just the
OIDC `sub` claim. Everything a user can do inside a home flows from an active
`Membership`.
"""

import hashlib
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import NewType
from uuid import UUID, uuid4

from smarthome.modules.identity.domain.errors import (
    InvalidInvitation,
    InvalidMembership,
    MembershipNotActive,
)

UserId = NewType("UserId", str)
HomeId = NewType("HomeId", UUID)

HOME_NAME_MAX_LENGTH = 80
INVITATION_TTL = timedelta(hours=72)
MAX_GUEST_ACCESS = timedelta(days=30)


class Role(StrEnum):
    OWNER = "owner"
    RESIDENT = "resident"
    GUEST = "guest"
    VIEWER = "viewer"


class Permission(StrEnum):
    VIEW_HOME = "view_home"
    CONTROL_DEVICES = "control_devices"
    OPERATE_LOCKS = "operate_locks"
    MANAGE_DEVICES = "manage_devices"
    MANAGE_AUTOMATIONS = "manage_automations"
    MANAGE_MEMBERS = "manage_members"
    VIEW_AUDIT_LOG = "view_audit_log"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.RESIDENT: frozenset(
        {
            Permission.VIEW_HOME,
            Permission.CONTROL_DEVICES,
            Permission.OPERATE_LOCKS,
            Permission.MANAGE_DEVICES,
            Permission.MANAGE_AUTOMATIONS,
        }
    ),
    # A guest can operate devices (it is the point of a guest pass), but only those in
    # their device scope and only until the pass expires. Locks need an explicit scope.
    Role.GUEST: frozenset(
        {Permission.VIEW_HOME, Permission.CONTROL_DEVICES, Permission.OPERATE_LOCKS}
    ),
    Role.VIEWER: frozenset({Permission.VIEW_HOME}),
}

# Permissions that act on a specific device and are therefore subject to a guest's scope.
DEVICE_PERMISSIONS = frozenset({Permission.CONTROL_DEVICES, Permission.OPERATE_LOCKS})


@dataclass(frozen=True, slots=True)
class Home:
    id: HomeId
    name: str
    timezone: str
    created_by: UserId
    created_at: datetime

    @staticmethod
    def create(*, name: str, timezone: str, created_by: UserId, now: datetime) -> "Home":
        cleaned = name.strip()
        if not 1 <= len(cleaned) <= HOME_NAME_MAX_LENGTH:
            raise InvalidMembership(f"home name must be 1-{HOME_NAME_MAX_LENGTH} characters")
        return Home(
            id=HomeId(uuid4()),
            name=cleaned,
            timezone=timezone,
            created_by=created_by,
            created_at=now,
        )


@dataclass(frozen=True, slots=True)
class Membership:
    id: UUID
    home_id: HomeId
    user_id: UserId
    role: Role
    granted_by: UserId
    granted_at: datetime
    expires_at: datetime | None = None
    device_scope: frozenset[str] | None = None
    revoked_at: datetime | None = None
    revoked_by: UserId | None = None

    def __post_init__(self) -> None:
        # Mirrors the CHECK constraints on identity.memberships.
        if self.role is Role.GUEST and self.expires_at is None:
            raise InvalidMembership("guest access must expire")
        if self.role is Role.OWNER and self.expires_at is not None:
            raise InvalidMembership("owners do not expire")
        if self.device_scope is not None and self.role is not Role.GUEST:
            raise InvalidMembership("only guests can be limited to specific devices")
        if self.expires_at is not None and self.expires_at <= self.granted_at:
            raise InvalidMembership("access must expire after it is granted")

    @staticmethod
    def grant(
        *,
        home_id: HomeId,
        user_id: UserId,
        role: Role,
        granted_by: UserId,
        now: datetime,
        expires_at: datetime | None = None,
        device_scope: frozenset[str] | None = None,
    ) -> "Membership":
        return Membership(
            id=uuid4(),
            home_id=home_id,
            user_id=user_id,
            role=role,
            granted_by=granted_by,
            granted_at=now,
            expires_at=expires_at,
            device_scope=device_scope,
        )

    def is_active(self, now: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or now < self.expires_at

    def allows(
        self, permission: Permission, *, now: datetime, device_id: str | None = None
    ) -> bool:
        if not self.is_active(now):
            return False
        if permission not in ROLE_PERMISSIONS[self.role]:
            return False
        if self.device_scope is None or permission not in DEVICE_PERMISSIONS:
            return True
        # A scoped guest needs a concrete device, and it must be in scope.
        return device_id is not None and device_id in self.device_scope

    def revoke(self, *, by: UserId, now: datetime) -> "Membership":
        if not self.is_active(now):
            raise MembershipNotActive("membership is already revoked or expired")
        return replace(self, revoked_at=now, revoked_by=by)

    def close_expired(self, *, now: datetime) -> "Membership":
        """Close a membership that lapsed on its own, so a new one can take its place.

        Nobody revoked it, so the member is recorded as the closing actor.
        """
        if self.revoked_at is not None or self.is_active(now):
            raise MembershipNotActive("only an expired, unrevoked membership can be closed")
        return replace(self, revoked_at=now, revoked_by=self.user_id)


def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True, slots=True)
class Invitation:
    """A single-use link that turns whoever redeems it into a member of the home.

    Only the sha256 of the token is stored; the plaintext is shown once, to the inviter.
    """

    id: UUID
    home_id: HomeId
    role: Role
    token_hash: bytes
    invited_by: UserId
    created_at: datetime
    expires_at: datetime
    guest_access_expires_at: datetime | None = None
    device_scope: frozenset[str] | None = None
    accepted_by: UserId | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None

    @staticmethod
    def issue(
        *,
        home_id: HomeId,
        role: Role,
        invited_by: UserId,
        now: datetime,
        guest_access_expires_at: datetime | None = None,
        device_scope: frozenset[str] | None = None,
    ) -> tuple["Invitation", str]:
        if role is Role.OWNER:
            raise InvalidInvitation("ownership cannot be granted through an invitation")
        if role is Role.GUEST:
            if guest_access_expires_at is None:
                raise InvalidInvitation("a guest invitation must say when access ends")
            if not now < guest_access_expires_at <= now + MAX_GUEST_ACCESS:
                raise InvalidInvitation("guest access must end within 30 days")
        elif guest_access_expires_at is not None or device_scope is not None:
            raise InvalidInvitation("only guest invitations carry an expiry or a device scope")
        if device_scope is not None and not device_scope:
            raise InvalidInvitation("a device scope cannot be empty")

        token = secrets.token_urlsafe(32)
        invitation = Invitation(
            id=uuid4(),
            home_id=home_id,
            role=role,
            token_hash=_hash_token(token),
            invited_by=invited_by,
            created_at=now,
            expires_at=now + INVITATION_TTL,
            guest_access_expires_at=guest_access_expires_at,
            device_scope=device_scope,
        )
        return invitation, token

    @staticmethod
    def hash_token(token: str) -> bytes:
        return _hash_token(token)

    def is_redeemable(self, now: datetime) -> bool:
        return self.accepted_at is None and self.revoked_at is None and now < self.expires_at

    def accept(self, *, by: UserId, now: datetime) -> tuple["Invitation", Membership]:
        if not self.is_redeemable(now):
            raise InvalidInvitation("invitation is expired, revoked or already used")
        if self.guest_access_expires_at is not None and self.guest_access_expires_at <= now:
            raise InvalidInvitation("the guest access window has already ended")
        membership = Membership.grant(
            home_id=self.home_id,
            user_id=by,
            role=self.role,
            granted_by=self.invited_by,
            now=now,
            expires_at=self.guest_access_expires_at,
            device_scope=self.device_scope,
        )
        return replace(self, accepted_by=by, accepted_at=now), membership
