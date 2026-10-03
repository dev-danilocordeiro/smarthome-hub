"""Automations, scenes and runs."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from device_protocol import DeviceKind
from smarthome.modules.automations.domain.definition import Definition, check_desired
from smarthome.modules.automations.domain.errors import InvalidDefinition

NAME_MAX_LENGTH = 80
DESCRIPTION_MAX_LENGTH = 500
SCENE_MAX_DEVICES = 50


def _name(raw: str) -> str:
    cleaned = raw.strip()
    if not 1 <= len(cleaned) <= NAME_MAX_LENGTH:
        raise InvalidDefinition(f"name must be 1-{NAME_MAX_LENGTH} characters", path="name")
    return cleaned


def _description(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    if len(raw) > DESCRIPTION_MAX_LENGTH:
        raise InvalidDefinition(
            f"description must be at most {DESCRIPTION_MAX_LENGTH} characters", path="description"
        )
    return raw.strip()


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidDefinition(f"unknown time zone {name!r}") from exc


class AutomationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"  # stopped by the loop guard until someone re-enables it


@dataclass(frozen=True, slots=True)
class Automation:
    id: UUID
    home_id: UUID
    name: str
    description: str | None
    enabled: bool
    status: AutomationStatus
    version: int
    definition: Definition
    # The home's zone when it was saved; schedules and time conditions use it.
    timezone: str
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    status_reason: str | None = None

    @staticmethod
    def create(
        *,
        home_id: UUID,
        name: str,
        description: str | None,
        enabled: bool,
        definition: Definition,
        timezone: str,
        by: str,
        now: datetime,
    ) -> "Automation":
        zone(timezone)
        return Automation(
            id=uuid4(),
            home_id=home_id,
            name=_name(name),
            description=_description(description),
            enabled=enabled,
            status=AutomationStatus.ACTIVE,
            version=1,
            definition=definition,
            timezone=timezone,
            created_by=by,
            created_at=now,
            updated_by=by,
            updated_at=now,
        )

    @property
    def armed(self) -> bool:
        return self.enabled and self.status is AutomationStatus.ACTIVE

    @property
    def zone(self) -> ZoneInfo:
        return zone(self.timezone)

    def revise(
        self,
        *,
        name: str,
        description: str | None,
        enabled: bool,
        definition: Definition,
        by: str,
        now: datetime,
    ) -> "Automation":
        """A new version. Saving an edit also lifts a suspension: the person who edits a
        suspended automation is the one who was asked to look at it."""
        return replace(
            self,
            name=_name(name),
            description=_description(description),
            enabled=enabled,
            definition=definition,
            version=self.version + 1,
            status=AutomationStatus.ACTIVE,
            status_reason=None,
            updated_by=by,
            updated_at=now,
        )

    def switch(self, *, enabled: bool, by: str, now: datetime) -> "Automation":
        """Enable (which also lifts a suspension) or disable. Not a new version: the
        definition does not change."""
        return replace(
            self,
            enabled=enabled,
            status=AutomationStatus.ACTIVE if enabled else self.status,
            status_reason=None if enabled else self.status_reason,
            updated_by=by,
            updated_at=now,
        )

    def suspend(self, *, reason: str, now: datetime) -> "Automation":
        return replace(
            self, status=AutomationStatus.SUSPENDED, status_reason=reason, updated_at=now
        )


@dataclass(frozen=True, slots=True)
class SceneState:
    device_id: str
    desired: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Scene:
    """A named set of desired states, applied together (one command per device)."""

    id: UUID
    home_id: UUID
    name: str
    states: tuple[SceneState, ...]
    version: int
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime

    @staticmethod
    def create(
        *, home_id: UUID, name: str, states: list[SceneState], by: str, now: datetime
    ) -> "Scene":
        return Scene(
            id=uuid4(),
            home_id=home_id,
            name=_name(name),
            states=_states(states),
            version=1,
            created_by=by,
            created_at=now,
            updated_by=by,
            updated_at=now,
        )

    def revise(self, *, name: str, states: list[SceneState], by: str, now: datetime) -> "Scene":
        return replace(
            self,
            name=_name(name),
            states=_states(states),
            version=self.version + 1,
            updated_by=by,
            updated_at=now,
        )

    @property
    def device_ids(self) -> frozenset[str]:
        return frozenset(s.device_id for s in self.states)

    def check_devices(self, kinds: Mapping[str, DeviceKind]) -> None:
        for i, state in enumerate(self.states):
            kind = kinds.get(state.device_id)
            if kind is None:
                raise InvalidDefinition(
                    f"no device {state.device_id!r} in this home", path=f"states/{i}/device_id"
                )
            check_desired(kind, state.desired, path=f"states/{i}/desired")


def _states(states: list[SceneState]) -> tuple[SceneState, ...]:
    if not 1 <= len(states) <= SCENE_MAX_DEVICES:
        raise InvalidDefinition(f"a scene sets 1-{SCENE_MAX_DEVICES} devices", path="states")
    seen: set[str] = set()
    for i, state in enumerate(states):
        if state.device_id in seen:
            raise InvalidDefinition(
                f"{state.device_id} appears twice", path=f"states/{i}/device_id"
            )
        if not state.desired:
            raise InvalidDefinition("desired state is empty", path=f"states/{i}/desired")
        seen.add(state.device_id)
    return tuple(states)


class RunStatus(StrEnum):
    RUNNING = "running"  # recorded; its actions are being issued
    COMPLETED = "completed"  # every command was accepted for delivery
    FAILED = "failed"  # at least one action could not be issued
    SKIPPED = "skipped"  # a condition did not hold
    SUPPRESSED = "suppressed"  # cooldown or loop guard


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """One command issued (or not) by a run. Its delivery is tracked by the command."""

    device_id: str
    command_id: UUID | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Run:
    id: UUID
    automation_id: UUID
    home_id: UUID
    automation_version: int
    trigger_index: int
    # Unique per automation: the stream entry, timer or schedule slot that fired it.
    # A redelivered event finds its run already recorded and does nothing.
    event_key: str
    status: RunStatus
    depth: int
    started_at: datetime
    finished_at: datetime | None = None
    reason: str | None = None
    outcomes: tuple[ActionOutcome, ...] = field(default=())
    trace_id: str | None = None

    @staticmethod
    def start(
        automation: Automation,
        *,
        trigger_index: int,
        event_key: str,
        depth: int,
        now: datetime,
        status: RunStatus = RunStatus.RUNNING,
        reason: str | None = None,
    ) -> "Run":
        settled = status is not RunStatus.RUNNING
        return Run(
            id=uuid4(),
            automation_id=automation.id,
            home_id=automation.home_id,
            automation_version=automation.version,
            trigger_index=trigger_index,
            event_key=event_key,
            status=status,
            depth=depth,
            started_at=now,
            finished_at=now if settled else None,
            reason=reason,
        )

    def finish(self, outcomes: list[ActionOutcome], *, now: datetime) -> "Run":
        failed = [o for o in outcomes if o.error is not None]
        return replace(
            self,
            status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
            outcomes=tuple(outcomes),
            finished_at=now,
            reason=f"{len(failed)} of {len(outcomes)} actions failed" if failed else None,
        )

    def interrupt(self, *, now: datetime) -> "Run":
        return replace(
            self,
            status=RunStatus.FAILED,
            finished_at=now,
            reason="interrupted before its actions were confirmed",
        )
