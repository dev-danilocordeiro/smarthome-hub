"""Managing automations and scenes (called by the API)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from device_protocol import DeviceKind
from smarthome.modules.automations.application.actions import ActionRunner, scene_steps
from smarthome.modules.automations.application.ports import (
    AutomationsUnitOfWork,
    AutomationsUnitOfWorkFactory,
    Devices,
    Directory,
    Revision,
    TelemetryHistory,
)
from smarthome.modules.automations.domain.analysis import Node, loop_warnings
from smarthome.modules.automations.domain.definition import CommandStep, Definition
from smarthome.modules.automations.domain.dry_run import DryRun, replayable, simulate
from smarthome.modules.automations.domain.engine import Limits, prime
from smarthome.modules.automations.domain.errors import (
    AutomationNotFound,
    InvalidDefinition,
    SceneInUse,
    SceneNotFound,
    VersionMismatch,
)
from smarthome.modules.automations.domain.model import (
    ActionOutcome,
    Automation,
    Run,
    Scene,
    SceneState,
    zone,
)
from smarthome.modules.automations.domain.schedule import next_occurrence
from smarthome.shared.audit import AuditEvent
from smarthome.shared.clock import Clock

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Target:
    """A device an automation or scene would send a command to (for authorization)."""

    device_id: str
    kind: DeviceKind
    action: str


@dataclass(frozen=True, slots=True)
class Saved:
    automation: Automation
    warnings: list[str]


def _audit(
    action: str, actor: str, target_type: str, target_id: UUID, home_id: UUID, **details: Any
) -> AuditEvent:
    return AuditEvent(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        tenant_id=home_id,
        details=details,
    )


class AutomationsService:
    def __init__(
        self,
        uow: AutomationsUnitOfWorkFactory,
        *,
        directory: Directory,
        devices: Devices,
        history: TelemetryHistory,
        actions: ActionRunner,
        clock: Clock,
        limits: Limits,
    ) -> None:
        self._uow = uow
        self._directory = directory
        self._devices = devices
        self._history = history
        self._actions = actions
        self._clock = clock
        self._limits = limits

    # --- Reads --------------------------------------------------------------------------

    async def list_automations(self, home_id: UUID) -> list[Automation]:
        async with self._uow() as uow:
            return await uow.automations.for_home(home_id)

    async def get(self, home_id: UUID, automation_id: UUID) -> Automation:
        async with self._uow() as uow:
            automation = await uow.automations.get(automation_id)
        if automation is None or automation.home_id != home_id:
            raise AutomationNotFound(str(automation_id))
        return automation

    async def revisions(self, home_id: UUID, automation_id: UUID) -> list[Revision]:
        async with self._uow() as uow:
            revisions = await uow.revisions.for_automation(automation_id)
        if not revisions or any(r.home_id != home_id for r in revisions):
            raise AutomationNotFound(str(automation_id))
        return revisions

    async def runs(self, home_id: UUID, automation_id: UUID, *, limit: int) -> list[Run]:
        await self.get(home_id, automation_id)
        async with self._uow() as uow:
            return await uow.runs.for_automation(automation_id, limit=limit)

    # --- Validation ---------------------------------------------------------------------

    async def targets(self, home_id: UUID, raw: object) -> list[Target]:
        """Validate a definition against the home and list the commands it can send,
        so the API can check the editor may send them all."""
        definition, kinds, scenes = await self._validate(home_id, raw)
        return self._targets(definition, kinds, scenes)

    async def _validate(
        self, home_id: UUID, raw: object
    ) -> tuple[Definition, dict[str, DeviceKind], dict[UUID, Scene]]:
        definition = Definition.parse(raw)
        kinds = await self._devices.kinds(home_id)
        definition.check_devices(kinds)
        scenes = await self._scenes(home_id)
        for scene_id in definition.scene_ids():
            if scene_id not in scenes:
                raise InvalidDefinition(f"no scene {scene_id} in this home", path="actions")
        return definition, kinds, scenes

    @staticmethod
    def _targets(
        definition: Definition, kinds: dict[str, DeviceKind], scenes: dict[UUID, Scene]
    ) -> list[Target]:
        found = [
            Target(a.device_id, kinds[a.device_id], a.action)
            for a in definition.actions
            if isinstance(a, CommandStep)
        ]
        for scene_id in definition.scene_ids():
            found += [
                Target(s.device_id, kinds[s.device_id], "set_state")
                for s in scenes[scene_id].states
                if s.device_id in kinds
            ]
        return found

    async def _scenes(self, home_id: UUID) -> dict[UUID, Scene]:
        async with self._uow() as uow:
            return {s.id: s for s in await uow.scenes.for_home(home_id)}

    # --- Changes ------------------------------------------------------------------------

    async def create(
        self,
        *,
        home_id: UUID,
        timezone: str,
        name: str,
        description: str | None,
        enabled: bool,
        definition: object,
        actor: str,
    ) -> Saved:
        parsed, _, scenes = await self._validate(home_id, definition)
        now = self._clock.now()
        automation = Automation.create(
            home_id=home_id,
            name=name,
            description=description,
            enabled=enabled,
            definition=parsed,
            timezone=timezone,
            by=actor,
            now=now,
        )
        async with self._uow() as uow:
            await uow.automations.add(automation)
            await uow.revisions.add(automation, change="created", by=actor, at=now)
            await self._arm(uow, automation)
            await uow.audit.append(
                _audit(
                    "automation.created",
                    actor,
                    "automation",
                    automation.id,
                    home_id,
                    name=automation.name,
                    enabled=enabled,
                ),
                occurred_at=now,
            )
            await uow.commit()
        await self._directory.invalidate(home_id)
        return Saved(automation, await self._warnings(automation, scenes))

    async def update(
        self,
        *,
        home_id: UUID,
        automation_id: UUID,
        expected_version: int,
        name: str,
        description: str | None,
        enabled: bool,
        definition: object,
        actor: str,
    ) -> Saved:
        parsed, _, scenes = await self._validate(home_id, definition)
        now = self._clock.now()
        async with self._uow() as uow:
            current = await self._locked(uow, home_id, automation_id, expected_version)
            automation = current.revise(
                name=name,
                description=description,
                enabled=enabled,
                definition=parsed,
                by=actor,
                now=now,
            )
            await uow.automations.save(automation)
            await uow.revisions.add(automation, change="updated", by=actor, at=now)
            await self._arm(uow, automation)
            await uow.audit.append(
                _audit(
                    "automation.updated",
                    actor,
                    "automation",
                    automation.id,
                    home_id,
                    name=automation.name,
                    version=automation.version,
                    enabled=enabled,
                ),
                occurred_at=now,
            )
            await uow.commit()
        await self._directory.invalidate(home_id)
        return Saved(automation, await self._warnings(automation, scenes))

    async def set_enabled(
        self, *, home_id: UUID, automation_id: UUID, enabled: bool, actor: str
    ) -> Automation:
        now = self._clock.now()
        async with self._uow() as uow:
            current = await self._locked(uow, home_id, automation_id, None)
            automation = current.switch(enabled=enabled, by=actor, now=now)
            await uow.automations.save(automation)
            await self._arm(uow, automation)
            action = "automation.enabled" if enabled else "automation.disabled"
            await uow.audit.append(
                _audit(
                    action,
                    actor,
                    "automation",
                    automation.id,
                    home_id,
                    name=automation.name,
                    was_suspended=current.status_reason,
                ),
                occurred_at=now,
            )
            await uow.commit()
        await self._directory.invalidate(home_id)
        return automation

    async def delete(
        self, *, home_id: UUID, automation_id: UUID, expected_version: int | None, actor: str
    ) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            current = await self._locked(uow, home_id, automation_id, expected_version)
            await uow.revisions.add(
                current.revise(
                    name=current.name,
                    description=current.description,
                    enabled=False,
                    definition=current.definition,
                    by=actor,
                    now=now,
                ),
                change="deleted",
                by=actor,
                at=now,
            )
            await uow.trigger_states.replace(automation_id, {})
            await uow.schedules.replace(automation_id, {})
            await uow.automations.delete(automation_id)
            await uow.audit.append(
                _audit(
                    "automation.deleted",
                    actor,
                    "automation",
                    automation_id,
                    home_id,
                    name=current.name,
                ),
                occurred_at=now,
            )
            await uow.commit()
        await self._directory.invalidate(home_id)

    async def _locked(
        self,
        uow: AutomationsUnitOfWork,
        home_id: UUID,
        automation_id: UUID,
        expected_version: int | None,
    ) -> Automation:
        automation = await uow.automations.lock(automation_id)
        if automation is None or automation.home_id != home_id:
            raise AutomationNotFound(str(automation_id))
        if expected_version is not None and automation.version != expected_version:
            raise VersionMismatch(
                f"version {expected_version} is not current (now {automation.version})"
            )
        return automation

    async def _arm(self, uow: AutomationsUnitOfWork, automation: Automation) -> None:
        """Reset trigger state and schedules for the saved version. Trigger states are
        primed from the devices' current values, so the first real change fires."""
        if not automation.armed:
            await uow.trigger_states.replace(automation.id, {})
            await uow.schedules.replace(automation.id, {})
            return
        definition = automation.definition
        sources = [t.predicate.source for _, t in definition.device_triggers()]
        current = await self._devices.current(automation.home_id, sources)
        await uow.trigger_states.replace(automation.id, prime(definition, current))
        now = self._clock.now()
        await uow.schedules.replace(
            automation.id,
            {
                i: next_occurrence(t, automation.zone, after=now)
                for i, t in definition.schedule_triggers()
            },
        )

    async def _warnings(self, automation: Automation, scenes: dict[UUID, Scene]) -> list[str]:
        others = [a for a in await self.list_automations(automation.home_id) if a.armed]
        nodes = [Node(a.id, a.name, a.definition) for a in others if a.id != automation.id]
        nodes.append(Node(automation.id, automation.name, automation.definition))
        return loop_warnings(automation.id, nodes, scenes)

    # --- Dry run ------------------------------------------------------------------------

    async def dry_run(
        self,
        *,
        home_id: UUID,
        timezone: str,
        definition: object,
        start: datetime,
        end: datetime,
    ) -> DryRun:
        parsed, _, _ = await self._validate(home_id, definition)
        history = {
            source: await self._history.series(
                home_id, source.device_id, source.key, start=start, end=end
            )
            for source in replayable(parsed)
        }
        return simulate(
            parsed, zone=zone(timezone), start=start, end=end, history=history, limits=self._limits
        )

    # --- Scenes -------------------------------------------------------------------------

    async def list_scenes(self, home_id: UUID) -> list[Scene]:
        async with self._uow() as uow:
            return await uow.scenes.for_home(home_id)

    async def get_scene(self, home_id: UUID, scene_id: UUID) -> Scene:
        async with self._uow() as uow:
            scene = await uow.scenes.get(scene_id)
        if scene is None or scene.home_id != home_id:
            raise SceneNotFound(str(scene_id))
        return scene

    async def scene_targets(self, home_id: UUID, states: list[SceneState]) -> list[Target]:
        kinds = await self._devices.kinds(home_id)
        Scene.create(
            home_id=home_id, name="check", states=states, by="", now=self._clock.now()
        ).check_devices(kinds)
        return [Target(s.device_id, kinds[s.device_id], "set_state") for s in states]

    async def create_scene(
        self, *, home_id: UUID, name: str, states: list[SceneState], actor: str
    ) -> Scene:
        now = self._clock.now()
        scene = Scene.create(home_id=home_id, name=name, states=states, by=actor, now=now)
        scene.check_devices(await self._devices.kinds(home_id))
        async with self._uow() as uow:
            await uow.scenes.add(scene)
            await uow.audit.append(
                _audit(
                    "scene.created",
                    actor,
                    "scene",
                    scene.id,
                    home_id,
                    name=scene.name,
                    devices=sorted(scene.device_ids),
                ),
                occurred_at=now,
            )
            await uow.commit()
        return scene

    async def update_scene(
        self,
        *,
        home_id: UUID,
        scene_id: UUID,
        expected_version: int,
        name: str,
        states: list[SceneState],
        actor: str,
    ) -> Scene:
        now = self._clock.now()
        kinds = await self._devices.kinds(home_id)
        async with self._uow() as uow:
            current = await uow.scenes.lock(scene_id)
            if current is None or current.home_id != home_id:
                raise SceneNotFound(str(scene_id))
            if current.version != expected_version:
                raise VersionMismatch(
                    f"version {expected_version} is not current (now {current.version})"
                )
            scene = current.revise(name=name, states=states, by=actor, now=now)
            scene.check_devices(kinds)
            await uow.scenes.save(scene)
            await uow.audit.append(
                _audit(
                    "scene.updated",
                    actor,
                    "scene",
                    scene.id,
                    home_id,
                    name=scene.name,
                    version=scene.version,
                    devices=sorted(scene.device_ids),
                ),
                occurred_at=now,
            )
            await uow.commit()
        return scene

    async def delete_scene(
        self, *, home_id: UUID, scene_id: UUID, expected_version: int | None, actor: str
    ) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            scene = await uow.scenes.lock(scene_id)
            if scene is None or scene.home_id != home_id:
                raise SceneNotFound(str(scene_id))
            if expected_version is not None and scene.version != expected_version:
                raise VersionMismatch(
                    f"version {expected_version} is not current (now {scene.version})"
                )
            users = await uow.automations.using_scene(home_id, scene_id)
            if users:
                names = ", ".join(sorted(repr(a.name) for a in users))
                raise SceneInUse(f"used by {names}")
            await uow.scenes.delete(scene_id)
            await uow.audit.append(
                _audit("scene.deleted", actor, "scene", scene_id, home_id, name=scene.name),
                occurred_at=now,
            )
            await uow.commit()

    async def activate_scene(
        self, *, home_id: UUID, scene_id: UUID, actor: str
    ) -> list[ActionOutcome]:
        scene = await self.get_scene(home_id, scene_id)
        outcomes = await self._actions.run(home_id, scene_steps(scene), actor=actor)
        now = self._clock.now()
        async with self._uow() as uow:
            await uow.audit.append(
                _audit(
                    "scene.activated",
                    actor,
                    "scene",
                    scene_id,
                    home_id,
                    name=scene.name,
                    commands=[str(o.command_id) for o in outcomes if o.command_id],
                    failed=[o.device_id for o in outcomes if o.error],
                ),
                occurred_at=now,
            )
            await uow.commit()
        return outcomes
