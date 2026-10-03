"""In-memory implementations of the automations ports."""

import copy
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from device_protocol import DeviceKind
from smarthome.modules.automations.application.actions import ActionRunner
from smarthome.modules.automations.application.engine import AutomationEngine
from smarthome.modules.automations.application.ports import Revision
from smarthome.modules.automations.application.services import AutomationsService
from smarthome.modules.automations.domain.definition import Source
from smarthome.modules.automations.domain.engine import (
    Limits,
    Observation,
    RecentRuns,
    TriggerState,
)
from smarthome.modules.automations.domain.errors import NameTaken
from smarthome.modules.automations.domain.model import (
    ActionOutcome,
    Automation,
    Run,
    RunStatus,
    Scene,
)
from smarthome.shared.audit import AuditEvent

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # a Monday
HOME = UUID("00000000-0000-4000-8000-000000000001")
ACTED = {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED}


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> datetime:
        self.current += timedelta(**delta)
        return self.current


@dataclass
class Store:
    automations: dict[UUID, Automation] = field(default_factory=dict)
    revisions: list[Revision] = field(default_factory=list)
    states: dict[tuple[UUID, int], TriggerState] = field(default_factory=dict)
    schedules: dict[tuple[UUID, int], datetime] = field(default_factory=dict)
    runs: dict[UUID, Run] = field(default_factory=dict)
    scenes: dict[UUID, Scene] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)


class _Automations:
    def __init__(self, store: Store) -> None:
        self.s = store

    def _unique(self, automation: Automation) -> None:
        for other in self.s.automations.values():
            same = other.home_id == automation.home_id and other.id != automation.id
            if same and other.name.lower() == automation.name.lower():
                raise NameTaken(automation.name)

    async def add(self, automation: Automation) -> None:
        self._unique(automation)
        self.s.automations[automation.id] = automation

    async def get(self, automation_id: UUID) -> Automation | None:
        return self.s.automations.get(automation_id)

    lock = get

    async def for_home(self, home_id: UUID) -> list[Automation]:
        return sorted(
            (a for a in self.s.automations.values() if a.home_id == home_id),
            key=lambda a: a.name.lower(),
        )

    async def save(self, automation: Automation) -> None:
        self._unique(automation)
        self.s.automations[automation.id] = automation

    async def delete(self, automation_id: UUID) -> None:
        del self.s.automations[automation_id]

    async def using_scene(self, home_id: UUID, scene_id: UUID) -> list[Automation]:
        return [
            a
            for a in self.s.automations.values()
            if a.home_id == home_id and scene_id in a.definition.scene_ids()
        ]


class _Revisions:
    def __init__(self, store: Store) -> None:
        self.s = store

    async def add(self, automation: Automation, *, change: str, by: str, at: datetime) -> None:
        self.s.revisions.append(
            Revision(
                automation.id,
                automation.version,
                automation.home_id,
                automation.name,
                automation.definition.raw,
                change,
                by,
                at,
            )
        )

    async def for_automation(self, automation_id: UUID) -> list[Revision]:
        found = [r for r in self.s.revisions if r.automation_id == automation_id]
        return sorted(found, key=lambda r: -r.version)


class _TriggerStates:
    def __init__(self, store: Store) -> None:
        self.s = store

    async def lock(self, automation_id: UUID, index: int) -> TriggerState:
        return self.s.states.setdefault((automation_id, index), TriggerState())

    async def save(self, automation_id: UUID, index: int, state: TriggerState) -> None:
        self.s.states[(automation_id, index)] = state

    async def replace(self, automation_id: UUID, states: Mapping[int, TriggerState]) -> None:
        for key in [k for k in self.s.states if k[0] == automation_id]:
            del self.s.states[key]
        for index, state in states.items():
            self.s.states[(automation_id, index)] = state

    async def due(self, now: datetime, *, limit: int) -> list[tuple[UUID, int]]:
        return [k for k, v in self.s.states.items() if v.due(now)][:limit]


class _Schedules:
    def __init__(self, store: Store) -> None:
        self.s = store

    async def replace(self, automation_id: UUID, next_at: Mapping[int, datetime]) -> None:
        for key in [k for k in self.s.schedules if k[0] == automation_id]:
            del self.s.schedules[key]
        for index, at in next_at.items():
            self.s.schedules[(automation_id, index)] = at

    async def lock(self, automation_id: UUID, index: int) -> datetime | None:
        return self.s.schedules.get((automation_id, index))

    async def set_next(self, automation_id: UUID, index: int, next_at: datetime) -> None:
        self.s.schedules[(automation_id, index)] = next_at

    async def due(self, now: datetime, *, limit: int) -> list[tuple[UUID, int]]:
        return [k for k, v in self.s.schedules.items() if v <= now][:limit]


class _Runs:
    def __init__(self, store: Store) -> None:
        self.s = store

    async def add(self, run: Run) -> bool:
        if any(
            r.automation_id == run.automation_id and r.event_key == run.event_key
            for r in self.s.runs.values()
        ):
            return False
        self.s.runs[run.id] = run
        return True

    async def save(self, run: Run) -> None:
        self.s.runs[run.id] = run

    async def recent(self, automation_id: UUID, *, now: datetime) -> RecentRuns:
        acted = [
            r
            for r in self.s.runs.values()
            if r.automation_id == automation_id and r.status in ACTED
        ]
        return RecentRuns(
            last_ran_at=max((r.started_at for r in acted), default=None),
            ran_last_minute=sum(1 for r in acted if r.started_at > now - timedelta(minutes=1)),
        )

    async def for_automation(self, automation_id: UUID, *, limit: int) -> list[Run]:
        found = [r for r in self.s.runs.values() if r.automation_id == automation_id]
        return sorted(found, key=lambda r: r.started_at, reverse=True)[:limit]

    async def lock_running_since(self, before: datetime, *, limit: int) -> list[Run]:
        return [
            r
            for r in self.s.runs.values()
            if r.status is RunStatus.RUNNING and r.started_at < before
        ][:limit]

    async def purge(self, before: datetime) -> int:
        old = [
            k
            for k, r in self.s.runs.items()
            if r.started_at < before and r.status is not RunStatus.RUNNING
        ]
        for key in old:
            del self.s.runs[key]
        return len(old)


class _Scenes:
    def __init__(self, store: Store) -> None:
        self.s = store

    async def add(self, scene: Scene) -> None:
        if any(
            o.home_id == scene.home_id and o.name.lower() == scene.name.lower()
            for o in self.s.scenes.values()
        ):
            raise NameTaken(scene.name)
        self.s.scenes[scene.id] = scene

    async def get(self, scene_id: UUID) -> Scene | None:
        return self.s.scenes.get(scene_id)

    lock = get

    async def for_home(self, home_id: UUID) -> list[Scene]:
        return [s for s in self.s.scenes.values() if s.home_id == home_id]

    async def save(self, scene: Scene) -> None:
        self.s.scenes[scene.id] = scene

    async def delete(self, scene_id: UUID) -> None:
        del self.s.scenes[scene_id]


class _Audit:
    def __init__(self, store: Store) -> None:
        self.s = store

    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None:
        self.s.audit.append(event)


class UoW:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.automations = _Automations(store)
        self.revisions = _Revisions(store)
        self.trigger_states = _TriggerStates(store)
        self.schedules = _Schedules(store)
        self.runs = _Runs(store)
        self.scenes = _Scenes(store)
        self.audit = _Audit(store)
        self._snapshot: dict[str, Any] = {}
        self._committed = False

    async def __aenter__(self) -> Self:
        self._snapshot = copy.deepcopy(self.store.__dict__)
        return self

    async def commit(self) -> None:
        self._committed = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._committed:
            self.store.__dict__.update(self._snapshot)


class Directory:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.invalidated: list[UUID] = []

    async def armed(self, home_id: UUID) -> list[Automation]:
        return [a for a in self.store.automations.values() if a.home_id == home_id and a.armed]

    async def invalidate(self, home_id: UUID) -> None:
        self.invalidated.append(home_id)


class Devices:
    def __init__(self) -> None:
        self.kinds_by_id: dict[str, DeviceKind] = {
            "light-1": DeviceKind.LIGHT,
            "light-2": DeviceKind.LIGHT,
            "plug-1": DeviceKind.PLUG,
            "lock-1": DeviceKind.LOCK,
            "motion-1": DeviceKind.MOTION_SENSOR,
            "climate-1": DeviceKind.CLIMATE_SENSOR,
        }
        self.values: dict[Source, Observation] = {}

    async def kinds(self, home_id: UUID) -> dict[str, DeviceKind]:
        return dict(self.kinds_by_id)

    async def current(
        self, home_id: UUID, sources: Collection[Source]
    ) -> dict[Source, Observation | None]:
        return {s: self.values.get(s) for s in sources}


class History:
    def __init__(self) -> None:
        self.points: dict[tuple[str, str], list[tuple[datetime, float]]] = {}

    async def series(
        self, home_id: UUID, device_id: str, metric: str, *, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        return [p for p in self.points.get((device_id, metric), []) if start <= p[0] < end]


class Commands:
    def __init__(self, causality: "Causality | None" = None) -> None:
        self.issued: list[dict[str, Any]] = []
        self.refuse: set[str] = set()
        self.causality = causality
        self.depth_seen: list[int] = []

    async def issue(
        self,
        *,
        home_id: UUID,
        device_id: str,
        action: str,
        desired: dict[str, Any] | None,
        actor: str,
    ) -> ActionOutcome:
        if self.causality is not None:
            self.depth_seen.append(await self.causality.depth(device_id))
        if device_id in self.refuse:
            return ActionOutcome(device_id, error="TargetUnavailable: quarantined")
        self.issued.append(
            {"device_id": device_id, "action": action, "desired": desired, "actor": actor}
        )
        return ActionOutcome(device_id, command_id=uuid4())


class Causality:
    def __init__(self) -> None:
        self.depths: dict[str, int] = {}

    async def depth(self, device_id: str) -> int:
        return self.depths.get(device_id, 0)

    async def caused(self, device_ids: Collection[str], *, depth: int) -> None:
        for device_id in device_ids:
            self.depths[device_id] = depth


@dataclass
class Harness:
    store: Store
    clock: Clock
    directory: Directory
    devices: Devices
    history: History
    commands: Commands
    causality: Causality
    service: AutomationsService
    engine: AutomationEngine


def harness(limits: Limits | None = None) -> Harness:
    store = Store()
    clock = Clock()
    directory = Directory(store)
    devices = Devices()
    history = History()
    causality = Causality()
    commands = Commands(causality)

    def uow() -> UoW:
        return UoW(store)

    actions = ActionRunner(uow, commands)
    resolved = limits or Limits()
    service = AutomationsService(
        uow,
        directory=directory,
        devices=devices,
        history=history,
        actions=actions,
        clock=clock,
        limits=resolved,
    )
    engine = AutomationEngine(
        uow,
        directory=directory,
        devices=devices,
        actions=actions,
        causality=causality,
        clock=clock,
        limits=resolved,
    )
    return Harness(store, clock, directory, devices, history, commands, causality, service, engine)
