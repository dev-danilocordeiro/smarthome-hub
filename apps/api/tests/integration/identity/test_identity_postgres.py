"""The identity service against real Postgres, including the races its locks exist for."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.modules.identity.application.services import IdentityService
from smarthome.modules.identity.domain.errors import InvalidInvitation, LastOwner
from smarthome.modules.identity.domain.model import Home, Permission, Role, UserId
from smarthome.modules.identity.domain.principal import Principal
from smarthome.modules.identity.infrastructure.persistence import PostgresUnitOfWork
from smarthome.shared.clock import SystemClock

RACE_ROUNDS = 10


def person(name: str) -> Principal:
    return Principal(
        user_id=UserId(name),
        email=None,
        display_name=name.title(),
        authenticated_at=SystemClock().now(),
    )


@pytest.fixture
def service(engine: AsyncEngine) -> IdentityService:
    return IdentityService(lambda: PostgresUnitOfWork(engine), SystemClock())


async def new_home(service: IdentityService, owner: Principal) -> Home:
    return await service.create_home(owner, name="Casa", timezone="UTC")


async def test_the_full_membership_lifecycle_round_trips_through_postgres(
    service: IdentityService,
) -> None:
    alice, carol = person("alice"), person("carol")
    home = await new_home(service, alice)
    _, token = await service.invite(
        alice,
        home.id,
        role=Role.GUEST,
        guest_access_expires_at=SystemClock().now() + timedelta(days=1),
        device_scope=frozenset({"front-door"}),
    )

    access = await service.accept_invitation(carol, token)

    assert access.membership.device_scope == frozenset({"front-door"})
    members = {
        m.membership.user_id: m.membership.role for m in await service.members(alice, home.id)
    }
    assert members == {"alice": Role.OWNER, "carol": Role.GUEST}
    granted = await service.access(carol, home.id, Permission.OPERATE_LOCKS, device_id="front-door")
    assert granted.role is Role.GUEST

    log = await service.audit_log(alice, home.id, limit=10)
    assert log.chain_intact
    assert [e.action for e in log.entries] == [
        "home.created",
        "invitation.created",
        "invitation.accepted",
    ]


async def test_two_people_racing_to_redeem_one_invitation_cannot_both_get_in(
    service: IdentityService,
) -> None:
    for _ in range(RACE_ROUNDS):
        alice = person("alice")
        home = await new_home(service, alice)
        _, token = await service.invite(alice, home.id, role=Role.RESIDENT)

        results = await asyncio.gather(
            service.accept_invitation(person("bob"), token),
            service.accept_invitation(person("mallory"), token),
            return_exceptions=True,
        )

        assert sum(not isinstance(r, Exception) for r in results) == 1
        assert sum(isinstance(r, InvalidInvitation) for r in results) == 1
        assert len(await service.members(alice, home.id)) == 2


async def test_two_owners_removing_each_other_at_once_leave_the_home_with_an_owner(
    service: IdentityService,
) -> None:
    for _ in range(RACE_ROUNDS):
        alice, bob = person("alice"), person("bob")
        home = await new_home(service, alice)
        _, token = await service.invite(alice, home.id, role=Role.RESIDENT)
        await service.accept_invitation(bob, token)
        await _promote_to_owner(service, home, bob)

        results = await asyncio.gather(
            service.revoke_member(alice, home.id, bob.user_id),
            service.revoke_member(bob, home.id, alice.user_id),
            return_exceptions=True,
        )

        # Exactly one removal wins. The loser either saw two owners, waited on the row lock
        # and then found only one (LastOwner), or had already been removed (HomeNotFound).
        outcomes = sorted(type(r).__name__ for r in results)
        assert outcomes in (["LastOwner", "NoneType"], ["HomeNotFound", "NoneType"])
        owners = [
            m for m in await _members_as_superuser(service, home) if m.membership.role is Role.OWNER
        ]
        assert len(owners) == 1


async def test_the_last_owner_is_protected_in_postgres_too(service: IdentityService) -> None:
    alice = person("alice")
    home = await new_home(service, alice)

    with pytest.raises(LastOwner):
        await service.revoke_member(alice, home.id, alice.user_id)


async def _promote_to_owner(service: IdentityService, home: Home, who: Principal) -> None:
    # Ownership transfer is not a feature yet; set it up directly for the race test.
    from sqlalchemy import text  # noqa: PLC0415

    uow_factory = service._uow
    async with uow_factory() as uow:
        conn = uow._conn  # type: ignore[attr-defined]
        await conn.execute(
            text(
                "UPDATE identity.memberships SET role = 'owner' "
                "WHERE home_id = :h AND user_id = :u AND revoked_at IS NULL"
            ),
            {"h": home.id, "u": who.user_id},
        )
        await uow.commit()


async def _members_as_superuser(service: IdentityService, home: Home) -> list:  # type: ignore[type-arg]
    async with service._uow() as uow:
        current = await uow.memberships.current_for_home(home.id)
    from smarthome.modules.identity.application.services import MemberView  # noqa: PLC0415

    return [MemberView(m, None) for m in current]
