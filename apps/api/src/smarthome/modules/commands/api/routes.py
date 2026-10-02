from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from smarthome.modules.commands.api.container import CommandsModule
from smarthome.modules.commands.api.schemas import CommandCreate, CommandOut
from smarthome.modules.commands.domain.errors import TargetNotFound
from smarthome.modules.commands.domain.model import Capability, Command
from smarthome.modules.identity.public import (
    CurrentPrincipal,
    HomeAccess,
    Permission,
    require_device_access,
)

router = APIRouter(tags=["commands"])

PERMISSION_FOR: dict[Capability, Permission] = {
    Capability.CONTROL: Permission.CONTROL_DEVICES,
    Capability.OPERATE_SECURITY: Permission.OPERATE_LOCKS,
    Capability.MAINTAIN: Permission.MANAGE_DEVICES,
}


class NotAllowed(Exception):
    pass


class ReauthenticationRequired(Exception):
    pass


def commands_module(request: Request) -> CommandsModule:
    module: CommandsModule = request.app.state.commands
    return module


Commands = Annotated[CommandsModule, Depends(commands_module)]
CanView = Annotated[HomeAccess, Depends(require_device_access(Permission.VIEW_HOME))]
CanControl = Annotated[HomeAccess, Depends(require_device_access(Permission.CONTROL_DEVICES))]


def _out(command: Command) -> CommandOut:
    return CommandOut(
        id=command.id,
        device_id=command.device_id,
        action=command.action,
        desired=command.desired,
        status=command.status,
        issued_by=command.issued_by,
        issued_at=command.issued_at,
        expires_at=command.expires_at,
        delivered_at=command.delivered_at,
        completed_at=command.completed_at,
        reason=command.reason,
        trace_id=command.trace_id,
    )


def _in_scope(access: HomeAccess, device_id: str) -> None:
    scope = access.membership.device_scope
    if scope is not None and device_id not in scope:
        raise TargetNotFound(device_id)


@router.post(
    "/homes/{home_id}/devices/{device_id}/commands",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"description": "Locks and cameras: sign in again (reauthentication_required)"},
        409: {"description": "The device is quarantined"},
    },
)
async def issue(
    *,
    device_id: str,
    body: CommandCreate,
    access: CanControl,
    principal: CurrentPrincipal,
    module: Commands,
    response: Response,
) -> CommandOut:
    """Queue a command for the device. 202: it is on its way, not yet applied; poll the
    returned command (or watch the twin) for the outcome."""
    needs = await module.service.requirements(access.home.id, device_id, body.action)
    now = module.clock.now()
    missing = [
        PERMISSION_FOR[c].value
        for c in sorted(needs.capabilities)
        if not access.allows(PERMISSION_FOR[c], now=now, device_id=device_id)
    ]
    if missing:
        raise NotAllowed(f"{access.role.value} lacks {', '.join(missing)}")
    if needs.recent_authentication is not None and not principal.authenticated_within(
        needs.recent_authentication, now=now
    ):
        raise ReauthenticationRequired
    command = await module.service.issue(
        home_id=access.home.id,
        device_id=device_id,
        action=body.action,
        desired=body.desired,
        actor=access.membership.user_id,
        ttl=timedelta(seconds=body.ttl_s),
    )
    response.headers["Location"] = (
        f"/homes/{access.home.id}/devices/{device_id}/commands/{command.id}"
    )
    return _out(command)


@router.get("/homes/{home_id}/devices/{device_id}/commands")
async def recent(
    device_id: str,
    access: CanView,
    module: Commands,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[CommandOut]:
    _in_scope(access, device_id)
    return [_out(c) for c in await module.service.recent(access.home.id, device_id, limit=limit)]


@router.get("/homes/{home_id}/devices/{device_id}/commands/{command_id}")
async def get(device_id: str, command_id: UUID, access: CanView, module: Commands) -> CommandOut:
    _in_scope(access, device_id)
    return _out(await module.service.get(access.home.id, device_id, command_id))
