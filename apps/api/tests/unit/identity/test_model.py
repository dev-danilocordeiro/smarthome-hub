from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from smarthome.modules.identity.domain.errors import (
    InvalidInvitation,
    InvalidMembership,
    MembershipNotActive,
)
from smarthome.modules.identity.domain.model import (
    DEVICE_PERMISSIONS,
    ROLE_PERMISSIONS,
    HomeId,
    Invitation,
    Membership,
    Permission,
    Role,
    UserId,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
HOME = HomeId(uuid4())
ALICE = UserId("alice")
BOB = UserId("bob")


def membership(role: Role, **kwargs: object) -> Membership:
    if role is Role.GUEST:
        kwargs.setdefault("expires_at", NOW + timedelta(days=1))
    return Membership.grant(
        home_id=HOME,
        user_id=BOB,
        role=role,
        granted_by=ALICE,
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


@given(role=st.sampled_from(Role), permission=st.sampled_from(Permission))
def test_an_active_unscoped_member_has_exactly_the_permissions_of_their_role(
    role: Role, permission: Permission
) -> None:
    m = membership(role)

    allowed = m.allows(permission, now=NOW, device_id="lamp-1")

    assert allowed == (permission in ROLE_PERMISSIONS[role])


@given(
    role=st.sampled_from(Role),
    permission=st.sampled_from(Permission),
    after=st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=400)),
)
def test_a_revoked_member_is_allowed_nothing_ever_again(
    role: Role, permission: Permission, after: timedelta
) -> None:
    revoked = membership(role).revoke(by=ALICE, now=NOW)

    assert not revoked.allows(permission, now=NOW + after)


@given(
    permission=st.sampled_from(Permission),
    after=st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=400)),
)
def test_a_guest_pass_grants_nothing_from_the_moment_it_expires(
    permission: Permission, after: timedelta
) -> None:
    expires_at = NOW + timedelta(hours=2)
    guest = membership(Role.GUEST, expires_at=expires_at)

    assert not guest.allows(permission, now=expires_at + after)


def test_a_scoped_guest_controls_only_the_devices_in_scope() -> None:
    guest = membership(Role.GUEST, device_scope=frozenset({"front-door", "porch-light"}))

    assert guest.allows(Permission.OPERATE_LOCKS, now=NOW, device_id="front-door")
    assert guest.allows(Permission.CONTROL_DEVICES, now=NOW, device_id="porch-light")
    assert not guest.allows(Permission.CONTROL_DEVICES, now=NOW, device_id="garage-door")


def test_a_scoped_guest_cannot_act_on_devices_without_naming_one() -> None:
    guest = membership(Role.GUEST, device_scope=frozenset({"front-door"}))

    assert not guest.allows(Permission.CONTROL_DEVICES, now=NOW)
    assert guest.allows(Permission.VIEW_HOME, now=NOW)


def test_device_permissions_are_the_only_ones_a_scope_restricts() -> None:
    assert ROLE_PERMISSIONS[Role.GUEST] >= DEVICE_PERMISSIONS


@pytest.mark.parametrize(
    ("role", "kwargs", "message"),
    [
        (Role.GUEST, {"expires_at": None}, "guest access must expire"),
        (Role.OWNER, {"expires_at": NOW + timedelta(days=1)}, "owners do not expire"),
        (Role.RESIDENT, {"device_scope": frozenset({"x"})}, "only guests"),
        (Role.GUEST, {"expires_at": NOW}, "must expire after"),
    ],
)
def test_memberships_that_would_break_an_invariant_cannot_be_constructed(
    role: Role, kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidMembership, match=message):
        Membership.grant(
            home_id=HOME,
            user_id=BOB,
            role=role,
            granted_by=ALICE,
            now=NOW,
            **kwargs,  # type: ignore[arg-type]
        )


def test_revoking_twice_is_rejected() -> None:
    revoked = membership(Role.RESIDENT).revoke(by=ALICE, now=NOW)

    with pytest.raises(MembershipNotActive):
        revoked.revoke(by=ALICE, now=NOW)


def test_only_a_lapsed_membership_can_be_closed_as_expired() -> None:
    guest = membership(Role.GUEST, expires_at=NOW + timedelta(hours=1))

    with pytest.raises(MembershipNotActive):
        guest.close_expired(now=NOW)
    closed = guest.close_expired(now=NOW + timedelta(hours=2))
    assert closed.revoked_by == guest.user_id


def test_an_invitation_stores_only_a_hash_of_the_token_it_hands_out() -> None:
    invitation, token = Invitation.issue(
        home_id=HOME, role=Role.RESIDENT, invited_by=ALICE, now=NOW
    )

    assert token.encode() not in invitation.token_hash
    assert invitation.token_hash == Invitation.hash_token(token)
    assert len(token) >= 40


def test_accepting_an_invitation_grants_its_role_and_uses_it_up() -> None:
    invitation, _ = Invitation.issue(home_id=HOME, role=Role.VIEWER, invited_by=ALICE, now=NOW)

    accepted, member = invitation.accept(by=BOB, now=NOW + timedelta(minutes=5))

    assert member.role is Role.VIEWER
    assert member.granted_by == ALICE
    with pytest.raises(InvalidInvitation):
        accepted.accept(by=UserId("mallory"), now=NOW + timedelta(minutes=6))


def test_a_guest_invitation_carries_its_expiry_and_scope_into_the_membership() -> None:
    until = NOW + timedelta(days=2)
    invitation, _ = Invitation.issue(
        home_id=HOME,
        role=Role.GUEST,
        invited_by=ALICE,
        now=NOW,
        guest_access_expires_at=until,
        device_scope=frozenset({"front-door"}),
    )

    _, member = invitation.accept(by=BOB, now=NOW + timedelta(hours=1))

    assert member.expires_at == until
    assert member.device_scope == frozenset({"front-door"})


def test_an_invitation_cannot_be_redeemed_after_it_expires() -> None:
    invitation, _ = Invitation.issue(home_id=HOME, role=Role.RESIDENT, invited_by=ALICE, now=NOW)

    with pytest.raises(InvalidInvitation):
        invitation.accept(by=BOB, now=invitation.expires_at)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"role": Role.OWNER}, "ownership"),
        ({"role": Role.GUEST}, "must say when access ends"),
        (
            {"role": Role.GUEST, "guest_access_expires_at": NOW + timedelta(days=31)},
            "within 30 days",
        ),
        ({"role": Role.RESIDENT, "device_scope": frozenset({"x"})}, "only guest invitations"),
        (
            {
                "role": Role.GUEST,
                "guest_access_expires_at": NOW + timedelta(days=1),
                "device_scope": frozenset(),
            },
            "cannot be empty",
        ),
    ],
)
def test_invitations_that_would_grant_too_much_are_refused(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidInvitation, match=message):
        Invitation.issue(home_id=HOME, invited_by=ALICE, now=NOW, **kwargs)  # type: ignore[arg-type]
