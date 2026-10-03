from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from device_protocol import DeviceKind
from smarthome.modules.automations.domain.definition import Source
from smarthome.modules.automations.domain.engine import Observation, RecentRuns, TriggerState
from smarthome.modules.automations.domain.model import ActionOutcome, Automation, Run, Scene
from smarthome.shared.audit import AuditEvent


@dataclass(frozen=True, slots=True)
class Revision:
    automation_id: UUID
    version: int
    home_id: UUID
    name: str
    definition: dict[str, Any]
    change: str  # created | updated | deleted
    changed_by: str
    changed_at: datetime


class AutomationRepository(Protocol):
    async def add(self, automation: Automation) -> None:
        """Raises NameTaken."""
        ...

    async def get(self, automation_id: UUID) -> Automation | None: ...
    async def lock(self, automation_id: UUID) -> Automation | None: ...
    async def for_home(self, home_id: UUID) -> list[Automation]: ...
    async def save(self, automation: Automation) -> None:
        """Raises NameTaken."""
        ...

    async def delete(self, automation_id: UUID) -> None: ...
    async def using_scene(self, home_id: UUID, scene_id: UUID) -> list[Automation]: ...


class RevisionLog(Protocol):
    async def add(self, automation: Automation, *, change: str, by: str, at: datetime) -> None: ...
    async def for_automation(self, automation_id: UUID) -> list[Revision]: ...


class TriggerStates(Protocol):
    async def lock(self, automation_id: UUID, index: int) -> TriggerState:
        """The row locked for update (created as unknown if missing)."""
        ...

    async def save(self, automation_id: UUID, index: int, state: TriggerState) -> None: ...
    async def replace(self, automation_id: UUID, states: Mapping[int, TriggerState]) -> None:
        """Drop every state of the automation, then store these."""
        ...

    async def due(self, now: datetime, *, limit: int) -> list[tuple[UUID, int]]:
        """Hold timers that are due (not locked: the caller locks per automation)."""
        ...


class Schedules(Protocol):
    async def replace(self, automation_id: UUID, next_at: Mapping[int, datetime]) -> None: ...
    async def lock(self, automation_id: UUID, index: int) -> datetime | None: ...
    async def set_next(self, automation_id: UUID, index: int, next_at: datetime) -> None: ...
    async def due(self, now: datetime, *, limit: int) -> list[tuple[UUID, int]]: ...


class RunLog(Protocol):
    async def add(self, run: Run) -> bool:
        """False if this automation already has a run for the event key."""
        ...

    async def save(self, run: Run) -> None: ...
    async def recent(self, automation_id: UUID, *, now: datetime) -> RecentRuns: ...
    async def for_automation(self, automation_id: UUID, *, limit: int) -> list[Run]: ...
    async def lock_running_since(self, before: datetime, *, limit: int) -> list[Run]: ...
    async def purge(self, before: datetime) -> int: ...


class SceneRepository(Protocol):
    async def add(self, scene: Scene) -> None:
        """Raises NameTaken."""
        ...

    async def get(self, scene_id: UUID) -> Scene | None: ...
    async def lock(self, scene_id: UUID) -> Scene | None: ...
    async def for_home(self, home_id: UUID) -> list[Scene]: ...
    async def save(self, scene: Scene) -> None: ...
    async def delete(self, scene_id: UUID) -> None: ...


class AuditLog(Protocol):
    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None: ...


class AutomationsUnitOfWork(Protocol):
    @property
    def automations(self) -> AutomationRepository: ...
    @property
    def revisions(self) -> RevisionLog: ...
    @property
    def trigger_states(self) -> TriggerStates: ...
    @property
    def schedules(self) -> Schedules: ...
    @property
    def runs(self) -> RunLog: ...
    @property
    def scenes(self) -> SceneRepository: ...
    @property
    def audit(self) -> AuditLog: ...

    async def commit(self) -> None: ...
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class AutomationsUnitOfWorkFactory(Protocol):
    def __call__(self) -> AutomationsUnitOfWork: ...


# --- Other modules and Redis ------------------------------------------------------------


class Devices(Protocol):
    """The devices and telemetry modules, as far as automations are concerned."""

    async def kinds(self, home_id: UUID) -> dict[str, DeviceKind]:
        """Devices of the home that can be referenced (not revoked)."""
        ...

    async def current(
        self, home_id: UUID, sources: Collection[Source]
    ) -> dict[Source, Observation | None]:
        """What each source reads right now (twin, presence, latest reading)."""
        ...


class TelemetryHistory(Protocol):
    async def series(
        self, home_id: UUID, device_id: str, metric: str, *, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]: ...


class Commands(Protocol):
    async def issue(
        self,
        *,
        home_id: UUID,
        device_id: str,
        action: str,
        desired: dict[str, Any] | None,
        actor: str,
    ) -> ActionOutcome:
        """Never raises for a refused command: the outcome carries the error."""
        ...


class Causality(Protocol):
    """Which device changes were caused by automation runs, and how deep the chain is."""

    async def depth(self, device_id: str) -> int: ...
    async def caused(self, device_ids: Collection[str], *, depth: int) -> None: ...


class Directory(Protocol):
    """Armed automations per home, for the engine's hot path (cached)."""

    async def armed(self, home_id: UUID) -> list[Automation]: ...
    async def invalidate(self, home_id: UUID) -> None: ...
