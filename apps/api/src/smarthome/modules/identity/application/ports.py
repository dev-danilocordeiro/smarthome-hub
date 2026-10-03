from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from smarthome.modules.identity.domain.model import (
    Home,
    HomeId,
    Invitation,
    Membership,
    UserId,
)
from smarthome.shared.audit import AuditEntry, AuditEvent, ChainBreak


class HomeRepository(Protocol):
    async def add(self, home: Home) -> None: ...
    async def get(self, home_id: HomeId) -> Home | None: ...


class MembershipRepository(Protocol):
    async def add(self, membership: Membership) -> None: ...

    async def current(self, home_id: HomeId, user_id: UserId) -> Membership | None:
        """The unrevoked membership, if any (it may still have expired)."""
        ...

    async def current_for_user(self, user_id: UserId) -> list[tuple[Home, Membership]]: ...
    async def current_for_home(self, home_id: HomeId) -> list[Membership]: ...

    async def lock_current_for_home(self, home_id: HomeId) -> list[Membership]:
        """Like `current_for_home`, holding row locks until the unit of work ends."""
        ...

    async def save_revocation(self, membership: Membership) -> None: ...


class InvitationRepository(Protocol):
    async def add(self, invitation: Invitation) -> None: ...

    async def lock_by_token_hash(self, token_hash: bytes) -> Invitation | None:
        """Fetch and row-lock, so two people redeeming one link are serialized."""
        ...

    async def save_acceptance(self, invitation: Invitation) -> None: ...


class UserDirectory(Protocol):
    async def record_login(
        self, user_id: UserId, *, email: str | None, display_name: str | None, now: datetime
    ) -> None: ...

    async def display_names(self, user_ids: list[UserId]) -> dict[UserId, str]: ...
    async def emails(self, user_ids: list[UserId]) -> dict[UserId, str]:
        """The last email the IdP gave for each user that has one."""
        ...


class AuditTrail(Protocol):
    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None: ...
    async def recent(self, tenant_id: HomeId, *, limit: int) -> list[AuditEntry]: ...
    async def verify(self, tenant_id: HomeId) -> list[ChainBreak]: ...


class UnitOfWork(Protocol):
    """One database transaction. Leaving the block without `commit()` rolls back."""

    @property
    def homes(self) -> HomeRepository: ...
    @property
    def memberships(self) -> MembershipRepository: ...
    @property
    def invitations(self) -> InvitationRepository: ...
    @property
    def users(self) -> UserDirectory: ...
    @property
    def audit(self) -> AuditTrail: ...

    async def commit(self) -> None: ...
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
