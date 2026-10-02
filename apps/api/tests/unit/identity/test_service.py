from datetime import timedelta
from uuid import uuid4

import pytest

from smarthome.modules.identity.application.services import IdentityService
from smarthome.modules.identity.domain.errors import (
    AccessDenied,
    AlreadyMember,
    HomeNotFound,
    InvalidInvitation,
    LastOwner,
)
from smarthome.modules.identity.domain.model import Home, HomeId, Permission, Role, UserId
from smarthome.modules.identity.domain.principal import Principal
from tests.unit.identity.fakes import FakeClock, FakeUnitOfWork, Store


def principal(name: str, clock: FakeClock) -> Principal:
    return Principal(
        user_id=UserId(name),
        email=f"{name}@example.com",
        display_name=name.title(),
        authenticated_at=clock.now(),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def service(store: Store, clock: FakeClock) -> IdentityService:
    return IdentityService(lambda: FakeUnitOfWork(store), clock)


@pytest.fixture
async def home(service: IdentityService, clock: FakeClock) -> Home:
    return await service.create_home(principal("alice", clock), name="  Casa  ", timezone="UTC")


async def join(
    service: IdentityService, clock: FakeClock, home: Home, who: str, role: Role, **kw: object
) -> Principal:
    token = (await service.invite(principal("alice", clock), home.id, role=role, **kw))[1]  # type: ignore[arg-type]
    member = principal(who, clock)
    await service.accept_invitation(member, token)
    return member


async def test_creating_a_home_makes_the_creator_its_owner_and_audits_it(
    service: IdentityService, store: Store, clock: FakeClock, home: Home
) -> None:
    access = await service.access(principal("alice", clock), home.id, Permission.MANAGE_MEMBERS)

    assert home.name == "Casa"
    assert access.role is Role.OWNER
    assert [e.action for e in store.audit] == ["home.created"]
    assert store.audit[0].tenant_id == home.id


async def test_a_stranger_cannot_tell_a_foreign_home_from_a_missing_one(
    service: IdentityService, clock: FakeClock, home: Home
) -> None:
    mallory = principal("mallory", clock)

    with pytest.raises(HomeNotFound) as foreign:
        await service.access(mallory, home.id, Permission.VIEW_HOME)
    with pytest.raises(HomeNotFound) as missing:
        await service.access(mallory, HomeId(uuid4()), Permission.VIEW_HOME)
    assert type(foreign.value) is type(missing.value)


async def test_a_viewer_can_look_but_cannot_invite(
    service: IdentityService, clock: FakeClock, home: Home
) -> None:
    dave = await join(service, clock, home, "dave", Role.VIEWER)

    assert (await service.access(dave, home.id, Permission.VIEW_HOME)).role is Role.VIEWER
    with pytest.raises(AccessDenied):
        await service.invite(dave, home.id, role=Role.VIEWER)


async def test_an_invitation_link_works_exactly_once(
    service: IdentityService, clock: FakeClock, home: Home
) -> None:
    _, token = await service.invite(principal("alice", clock), home.id, role=Role.RESIDENT)
    await service.accept_invitation(principal("bob", clock), token)

    with pytest.raises(InvalidInvitation):
        await service.accept_invitation(principal("mallory", clock), token)


async def test_an_unknown_invitation_token_is_rejected_like_a_used_one(
    service: IdentityService, clock: FakeClock
) -> None:
    with pytest.raises(InvalidInvitation):
        await service.accept_invitation(principal("bob", clock), "x" * 43)


async def test_accepting_while_already_a_member_is_refused(
    service: IdentityService, clock: FakeClock, home: Home
) -> None:
    _, token = await service.invite(principal("alice", clock), home.id, role=Role.VIEWER)

    with pytest.raises(AlreadyMember):
        await service.accept_invitation(principal("alice", clock), token)


async def test_a_guest_loses_the_home_when_the_pass_expires_and_can_be_invited_again(
    service: IdentityService, clock: FakeClock, home: Home
) -> None:
    carol = await join(
        service,
        clock,
        home,
        "carol",
        Role.GUEST,
        guest_access_expires_at=clock.now() + timedelta(hours=4),
    )
    clock.advance(timedelta(hours=5))

    assert await service.my_homes(carol) == []
    with pytest.raises(HomeNotFound):
        await service.access(carol, home.id, Permission.VIEW_HOME)

    await join(
        service,
        clock,
        home,
        "carol",
        Role.GUEST,
        guest_access_expires_at=clock.now() + timedelta(hours=4),
    )
    assert [a.home.id for a in await service.my_homes(carol)] == [home.id]


async def test_the_last_owner_cannot_be_removed_or_leave(
    service: IdentityService, clock: FakeClock, home: Home
) -> None:
    alice = principal("alice", clock)

    with pytest.raises(LastOwner):
        await service.revoke_member(alice, home.id, alice.user_id)


async def test_an_owner_removes_a_member_who_then_loses_access_and_it_is_audited(
    service: IdentityService, store: Store, clock: FakeClock, home: Home
) -> None:
    bob = await join(service, clock, home, "bob", Role.RESIDENT)

    await service.revoke_member(principal("alice", clock), home.id, bob.user_id)

    with pytest.raises(HomeNotFound):
        await service.access(bob, home.id, Permission.VIEW_HOME)
    assert store.audit[-1].action == "membership.revoked"
    assert store.audit[-1].details == {"user_id": "bob", "role": "resident"}


async def test_a_member_can_leave_but_cannot_remove_others(
    service: IdentityService, clock: FakeClock, home: Home
) -> None:
    bob = await join(service, clock, home, "bob", Role.RESIDENT)
    dave = await join(service, clock, home, "dave", Role.VIEWER)

    with pytest.raises(AccessDenied):
        await service.revoke_member(dave, home.id, bob.user_id)
    await service.revoke_member(dave, home.id, dave.user_id)
    with pytest.raises(HomeNotFound):
        await service.access(dave, home.id, Permission.VIEW_HOME)


async def test_nothing_is_committed_when_a_rule_is_violated(
    service: IdentityService, store: Store, clock: FakeClock, home: Home
) -> None:
    commits = store.commits

    with pytest.raises(LastOwner):
        await service.revoke_member(principal("alice", clock), home.id, UserId("alice"))

    assert store.commits == commits
