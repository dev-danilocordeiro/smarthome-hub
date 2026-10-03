from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from smarthome.modules.automations.api.container import AutomationsModule
from smarthome.modules.automations.api.schemas import (
    ActionOutcomeOut,
    ActivationOut,
    AutomationIn,
    AutomationOut,
    DryRunIn,
    DryRunOut,
    RevisionOut,
    RunOut,
    SceneIn,
    SceneOut,
    SceneStateIn,
    SimulatedRunOut,
)
from smarthome.modules.automations.application.services import Target
from smarthome.modules.automations.domain.definition import definition_schema
from smarthome.modules.automations.domain.model import (
    ActionOutcome,
    Automation,
    Run,
    Scene,
    SceneState,
)
from smarthome.modules.commands.public import Capability, CommandAction, requirements
from smarthome.modules.identity.public import (
    CurrentPrincipal,
    HomeAccess,
    Permission,
    Principal,
    Role,
    require_home_access,
)
from smarthome.shared.http.preconditions import etag, expected_version

router = APIRouter(tags=["automations"])

PERMISSION_FOR: dict[Capability, Permission] = {
    Capability.CONTROL: Permission.CONTROL_DEVICES,
    Capability.OPERATE_SECURITY: Permission.OPERATE_LOCKS,
    Capability.MAINTAIN: Permission.MANAGE_DEVICES,
}


class NotAllowed(Exception):
    pass


class ReauthenticationRequired(Exception):
    pass


def automations_module(request: Request) -> AutomationsModule:
    module: AutomationsModule = request.app.state.automations
    return module


Automations = Annotated[AutomationsModule, Depends(automations_module)]
CanView = Annotated[HomeAccess, Depends(require_home_access(Permission.VIEW_HOME))]
CanManage = Annotated[HomeAccess, Depends(require_home_access(Permission.MANAGE_AUTOMATIONS))]


# --- Helpers ----------------------------------------------------------------------------


def authorize(
    access: HomeAccess, principal: Principal, targets: list[Target], *, now: datetime
) -> None:
    """Whoever saves an automation (or activates a scene) must be allowed to send every
    command in it themselves: an automation never grants more than its author has."""
    missing: set[str] = set()
    recent = None
    for target in targets:
        needs = requirements(target.kind, CommandAction(target.action))
        for capability in needs.capabilities:
            permission = PERMISSION_FOR[capability]
            if not access.allows(permission, now=now, device_id=target.device_id):
                missing.add(f"{permission.value} on {target.device_id}")
        if needs.recent_authentication is not None:
            recent = needs.recent_authentication
    if missing:
        raise NotAllowed(f"{access.role.value} lacks {', '.join(sorted(missing))}")
    if recent is not None and not principal.authenticated_within(recent, now=now):
        raise ReauthenticationRequired


def not_a_guest(access: HomeAccess) -> None:
    """Automations span the whole home; a guest pass covers only some devices."""
    if access.role is Role.GUEST:
        raise NotAllowed("guests cannot see automations")


def _automation_out(automation: Automation, warnings: list[str] | None = None) -> AutomationOut:
    return AutomationOut(
        id=automation.id,
        name=automation.name,
        description=automation.description,
        enabled=automation.enabled,
        status=automation.status,
        status_reason=automation.status_reason,
        version=automation.version,
        definition=automation.definition.raw,
        timezone=automation.timezone,
        created_by=automation.created_by,
        created_at=automation.created_at,
        updated_by=automation.updated_by,
        updated_at=automation.updated_at,
        warnings=warnings or [],
    )


def _outcome_out(outcome: ActionOutcome) -> ActionOutcomeOut:
    return ActionOutcomeOut(
        device_id=outcome.device_id, command_id=outcome.command_id, error=outcome.error
    )


def _run_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        automation_version=run.automation_version,
        trigger_index=run.trigger_index,
        status=run.status,
        reason=run.reason,
        depth=run.depth,
        started_at=run.started_at,
        finished_at=run.finished_at,
        outcomes=[_outcome_out(o) for o in run.outcomes],
        trace_id=run.trace_id,
    )


def _scene_out(scene: Scene) -> SceneOut:
    return SceneOut(
        id=scene.id,
        name=scene.name,
        states=[SceneStateIn(device_id=s.device_id, desired=s.desired) for s in scene.states],
        version=scene.version,
        created_by=scene.created_by,
        created_at=scene.created_at,
        updated_by=scene.updated_by,
        updated_at=scene.updated_at,
    )


def _states(body: SceneIn) -> list[SceneState]:
    return [SceneState(s.device_id, s.desired) for s in body.states]


# --- Automations ------------------------------------------------------------------------


@router.get("/automations/schema", response_model=None)
async def schema() -> dict[str, Any]:
    """JSON Schema (2020-12) of the automation definition, for editors and validators."""
    return definition_schema()


@router.get("/homes/{home_id}/automations")
async def list_automations(access: CanView, module: Automations) -> list[AutomationOut]:
    not_a_guest(access)
    return [_automation_out(a) for a in await module.service.list_automations(access.home.id)]


@router.post(
    "/homes/{home_id}/automations",
    status_code=status.HTTP_201_CREATED,
    responses={401: {"description": "Targets a lock or camera: sign in again"}},
)
async def create_automation(
    *,
    body: AutomationIn,
    access: CanManage,
    principal: CurrentPrincipal,
    module: Automations,
    response: Response,
) -> AutomationOut:
    home = access.home
    targets = await module.service.targets(home.id, body.definition)
    authorize(access, principal, targets, now=module.clock.now())
    saved = await module.service.create(
        home_id=home.id,
        timezone=home.timezone,
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        definition=body.definition,
        actor=access.membership.user_id,
    )
    response.headers["Location"] = f"/homes/{home.id}/automations/{saved.automation.id}"
    response.headers["ETag"] = etag(saved.automation.version)
    return _automation_out(saved.automation, saved.warnings)


@router.post("/homes/{home_id}/automations/dry-run")
async def dry_run(body: DryRunIn, access: CanManage, module: Automations) -> DryRunOut:
    """When would this definition have run? Replays recorded telemetry and the schedule
    (at most 7 days). Nothing is saved or sent."""
    result = await module.service.dry_run(
        home_id=access.home.id,
        timezone=access.home.timezone,
        definition=body.definition,
        start=body.start,
        end=body.end or datetime.now(UTC),
    )
    return DryRunOut(
        runs=[
            SimulatedRunOut(
                at=r.at,
                trigger_index=r.trigger_index,
                outcome=r.outcome,
                reason=r.reason,
                failed_conditions=list(r.failed_conditions),
                unknown_conditions=list(r.unknown_conditions),
            )
            for r in result.runs
        ],
        warnings=result.warnings,
        truncated=result.truncated,
    )


@router.get("/homes/{home_id}/automations/{automation_id}")
async def get_automation(
    automation_id: UUID, access: CanView, module: Automations, response: Response
) -> AutomationOut:
    not_a_guest(access)
    automation = await module.service.get(access.home.id, automation_id)
    response.headers["ETag"] = etag(automation.version)
    return _automation_out(automation)


@router.put(
    "/homes/{home_id}/automations/{automation_id}",
    responses={
        412: {"description": "If-Match does not name the current version"},
        428: {"description": "If-Match is missing"},
    },
)
async def update_automation(
    *,
    automation_id: UUID,
    body: AutomationIn,
    access: CanManage,
    principal: CurrentPrincipal,
    module: Automations,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> AutomationOut:
    version = expected_version(if_match, required=True)
    assert version is not None  # noqa: S101 - required
    targets = await module.service.targets(access.home.id, body.definition)
    authorize(access, principal, targets, now=module.clock.now())
    saved = await module.service.update(
        home_id=access.home.id,
        automation_id=automation_id,
        expected_version=version,
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        definition=body.definition,
        actor=access.membership.user_id,
    )
    response.headers["ETag"] = etag(saved.automation.version)
    return _automation_out(saved.automation, saved.warnings)


@router.delete(
    "/homes/{home_id}/automations/{automation_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_automation(
    automation_id: UUID,
    access: CanManage,
    module: Automations,
    if_match: Annotated[str | None, Header()] = None,
) -> None:
    await module.service.delete(
        home_id=access.home.id,
        automation_id=automation_id,
        expected_version=expected_version(if_match, required=False),
        actor=access.membership.user_id,
    )


@router.post("/homes/{home_id}/automations/{automation_id}/enable")
async def enable_automation(
    automation_id: UUID, access: CanManage, principal: CurrentPrincipal, module: Automations
) -> AutomationOut:
    """Arm it again; also lifts a suspension by the loop guard."""
    current = await module.service.get(access.home.id, automation_id)
    targets = await module.service.targets(access.home.id, current.definition.raw)
    authorize(access, principal, targets, now=module.clock.now())
    automation = await module.service.set_enabled(
        home_id=access.home.id,
        automation_id=automation_id,
        enabled=True,
        actor=access.membership.user_id,
    )
    return _automation_out(automation)


@router.post("/homes/{home_id}/automations/{automation_id}/disable")
async def disable_automation(
    automation_id: UUID, access: CanManage, module: Automations
) -> AutomationOut:
    automation = await module.service.set_enabled(
        home_id=access.home.id,
        automation_id=automation_id,
        enabled=False,
        actor=access.membership.user_id,
    )
    return _automation_out(automation)


@router.get("/homes/{home_id}/automations/{automation_id}/revisions")
async def revisions(automation_id: UUID, access: CanView, module: Automations) -> list[RevisionOut]:
    not_a_guest(access)
    return [
        RevisionOut(
            version=r.version,
            name=r.name,
            definition=r.definition,
            change=r.change,
            changed_by=r.changed_by,
            changed_at=r.changed_at,
        )
        for r in await module.service.revisions(access.home.id, automation_id)
    ]


@router.get("/homes/{home_id}/automations/{automation_id}/runs")
async def runs(
    automation_id: UUID,
    access: CanView,
    module: Automations,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunOut]:
    not_a_guest(access)
    found = await module.service.runs(access.home.id, automation_id, limit=limit)
    return [_run_out(r) for r in found]


# --- Scenes -----------------------------------------------------------------------------


@router.get("/homes/{home_id}/scenes")
async def list_scenes(access: CanView, module: Automations) -> list[SceneOut]:
    """A guest sees the scenes whose every device is within their pass."""
    scenes = await module.service.list_scenes(access.home.id)
    scope = access.membership.device_scope
    return [_scene_out(s) for s in scenes if scope is None or s.device_ids <= scope]


@router.post("/homes/{home_id}/scenes", status_code=status.HTTP_201_CREATED)
async def create_scene(
    *,
    body: SceneIn,
    access: CanManage,
    principal: CurrentPrincipal,
    module: Automations,
    response: Response,
) -> SceneOut:
    states = _states(body)
    targets = await module.service.scene_targets(access.home.id, states)
    authorize(access, principal, targets, now=module.clock.now())
    scene = await module.service.create_scene(
        home_id=access.home.id, name=body.name, states=states, actor=access.membership.user_id
    )
    response.headers["Location"] = f"/homes/{access.home.id}/scenes/{scene.id}"
    response.headers["ETag"] = etag(scene.version)
    return _scene_out(scene)


@router.get("/homes/{home_id}/scenes/{scene_id}")
async def get_scene(
    scene_id: UUID, access: CanView, module: Automations, response: Response
) -> SceneOut:
    scene = await module.service.get_scene(access.home.id, scene_id)
    scope = access.membership.device_scope
    if scope is not None and not scene.device_ids <= scope:
        raise NotAllowed("this scene includes devices outside your pass")
    response.headers["ETag"] = etag(scene.version)
    return _scene_out(scene)


@router.put("/homes/{home_id}/scenes/{scene_id}")
async def update_scene(
    *,
    scene_id: UUID,
    body: SceneIn,
    access: CanManage,
    principal: CurrentPrincipal,
    module: Automations,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> SceneOut:
    version = expected_version(if_match, required=True)
    assert version is not None  # noqa: S101 - required
    states = _states(body)
    targets = await module.service.scene_targets(access.home.id, states)
    authorize(access, principal, targets, now=module.clock.now())
    scene = await module.service.update_scene(
        home_id=access.home.id,
        scene_id=scene_id,
        expected_version=version,
        name=body.name,
        states=states,
        actor=access.membership.user_id,
    )
    response.headers["ETag"] = etag(scene.version)
    return _scene_out(scene)


@router.delete("/homes/{home_id}/scenes/{scene_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scene(
    scene_id: UUID,
    access: CanManage,
    module: Automations,
    if_match: Annotated[str | None, Header()] = None,
) -> None:
    await module.service.delete_scene(
        home_id=access.home.id,
        scene_id=scene_id,
        expected_version=expected_version(if_match, required=False),
        actor=access.membership.user_id,
    )


@router.post("/homes/{home_id}/scenes/{scene_id}/activate", status_code=status.HTTP_202_ACCEPTED)
async def activate_scene(
    scene_id: UUID,
    access: CanView,
    principal: CurrentPrincipal,
    module: Automations,
) -> ActivationOut:
    """One command per device, issued as you (so you need to be allowed to control each
    one; a guest only within their pass). 202: queued; follow each command for its
    outcome. A device that refuses (quarantined, unpaired) does not stop the others."""
    scene = await module.service.get_scene(access.home.id, scene_id)
    targets = await module.service.scene_targets(access.home.id, list(scene.states))
    authorize(access, principal, targets, now=module.clock.now())
    outcomes = await module.service.activate_scene(
        home_id=access.home.id, scene_id=scene_id, actor=access.membership.user_id
    )
    return ActivationOut(outcomes=[_outcome_out(o) for o in outcomes])
