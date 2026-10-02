from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from smarthome.modules.identity.api.dependencies import (
    CurrentPrincipal,
    Identity,
    require_home_access,
)
from smarthome.modules.identity.api.schemas import (
    AuditEntryOut,
    AuditLogOut,
    HomeCreate,
    HomeOut,
    InvitationAccept,
    InvitationCreate,
    InvitationOut,
    MemberOut,
)
from smarthome.modules.identity.application.services import HomeAccess
from smarthome.modules.identity.domain.model import HomeId, Permission, UserId

router = APIRouter(tags=["homes"])


def _home_out(access: HomeAccess) -> HomeOut:
    m = access.membership
    return HomeOut(
        id=access.home.id,
        name=access.home.name,
        timezone=access.home.timezone,
        role=m.role,
        access_expires_at=m.expires_at,
        device_scope=sorted(m.device_scope) if m.device_scope else None,
    )


@router.post("/homes", status_code=status.HTTP_201_CREATED)
async def create_home(body: HomeCreate, principal: CurrentPrincipal, module: Identity) -> HomeOut:
    """Create a home; the caller becomes its first owner."""
    home = await module.service.create_home(principal, name=body.name, timezone=body.timezone)
    return _home_out(await module.service.access(principal, home.id, Permission.VIEW_HOME))


@router.get("/homes")
async def list_homes(principal: CurrentPrincipal, module: Identity) -> list[HomeOut]:
    """Homes where the caller currently has access (expired guest passes are left out)."""
    return [_home_out(a) for a in await module.service.my_homes(principal)]


@router.get("/homes/{home_id}")
async def get_home(
    access: Annotated[HomeAccess, Depends(require_home_access(Permission.VIEW_HOME))],
) -> HomeOut:
    return _home_out(access)


@router.get("/homes/{home_id}/members")
async def list_members(
    home_id: UUID, principal: CurrentPrincipal, module: Identity
) -> list[MemberOut]:
    members = await module.service.members(principal, HomeId(home_id))
    return [
        MemberOut(
            user_id=v.membership.user_id,
            name=v.display_name,
            role=v.membership.role,
            granted_at=v.membership.granted_at,
            expires_at=v.membership.expires_at,
            device_scope=sorted(v.membership.device_scope) if v.membership.device_scope else None,
        )
        for v in members
    ]


@router.post("/homes/{home_id}/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    home_id: UUID, body: InvitationCreate, principal: CurrentPrincipal, module: Identity
) -> InvitationOut:
    invitation, token = await module.service.invite(
        principal,
        HomeId(home_id),
        role=body.role,
        guest_access_expires_at=body.guest_access_expires_at,
        device_scope=frozenset(body.device_scope) if body.device_scope else None,
    )
    return InvitationOut(
        id=invitation.id, role=invitation.role, expires_at=invitation.expires_at, token=token
    )


@router.post("/invitations/accept")
async def accept_invitation(
    body: InvitationAccept, principal: CurrentPrincipal, module: Identity
) -> HomeOut:
    return _home_out(await module.service.accept_invitation(principal, body.token))


@router.delete("/homes/{home_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    home_id: UUID, user_id: str, principal: CurrentPrincipal, module: Identity
) -> Response:
    """Remove a member (owners), or leave the home yourself (anyone)."""
    await module.service.revoke_member(principal, HomeId(home_id), UserId(user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/homes/{home_id}/audit")
async def audit_log(
    home_id: UUID,
    principal: CurrentPrincipal,
    module: Identity,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AuditLogOut:
    view = await module.service.audit_log(principal, HomeId(home_id), limit=limit)
    return AuditLogOut(
        chain_intact=view.chain_intact,
        entries=[
            AuditEntryOut(
                id=e.id,
                occurred_at=e.occurred_at,
                actor=e.actor,
                action=e.action,
                target_type=e.target_type,
                target_id=e.target_id,
                details=e.details,
                trace_id=e.trace_id,
            )
            for e in reversed(view.entries)
        ],
    )
