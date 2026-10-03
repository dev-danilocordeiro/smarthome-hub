"""Turning actions into commands: shared by automation runs and scene activation."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from smarthome.modules.automations.application.ports import AutomationsUnitOfWorkFactory, Commands
from smarthome.modules.automations.domain.definition import Action, CommandStep, SceneStep
from smarthome.modules.automations.domain.model import ActionOutcome, Scene


@dataclass(frozen=True, slots=True)
class Step:
    """One command to issue, or why it cannot be."""

    device_id: str
    action: str
    desired: dict[str, Any] | None
    error: str | None = None


def scene_steps(scene: Scene) -> list[Step]:
    return [Step(s.device_id, "set_state", dict(s.desired)) for s in scene.states]


class ActionRunner:
    def __init__(self, uow: AutomationsUnitOfWorkFactory, commands: Commands) -> None:
        self._uow = uow
        self._commands = commands

    async def plan(self, home_id: UUID, actions: tuple[Action, ...]) -> list[Step]:
        """Expand scenes into their commands. A scene deleted (or moved to another home)
        since the automation was saved becomes a failed step, not a crash."""
        steps: list[Step] = []
        scenes: dict[UUID, Scene | None] = {}
        for action in actions:
            if isinstance(action, CommandStep):
                steps.append(Step(action.device_id, action.action, action.desired))
                continue
            assert isinstance(action, SceneStep)  # noqa: S101 - the Action union
            if action.scene_id not in scenes:
                async with self._uow() as uow:
                    scene = await uow.scenes.get(action.scene_id)
                scenes[action.scene_id] = scene if scene and scene.home_id == home_id else None
            scene = scenes[action.scene_id]
            if scene is None:
                steps.append(Step(f"scene:{action.scene_id}", "set_state", None, "scene deleted"))
            else:
                steps.extend(scene_steps(scene))
        return steps

    async def run(self, home_id: UUID, steps: list[Step], *, actor: str) -> list[ActionOutcome]:
        """Issue in order. One refused command does not stop the others."""
        outcomes: list[ActionOutcome] = []
        for step in steps:
            if step.error is not None:
                outcomes.append(ActionOutcome(step.device_id, error=step.error))
                continue
            outcomes.append(
                await self._commands.issue(
                    home_id=home_id,
                    device_id=step.device_id,
                    action=step.action,
                    desired=step.desired,
                    actor=actor,
                )
            )
        return outcomes
