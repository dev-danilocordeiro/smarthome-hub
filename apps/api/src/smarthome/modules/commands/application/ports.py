from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from device_protocol import DeviceKind
from smarthome.modules.commands.domain.model import Command
from smarthome.shared.audit import AuditEvent
from smarthome.shared.outbox import OutboxMessage


class CommandRepository(Protocol):
    async def add(self, command: Command) -> str | None:
        """Insert; returns the trace id recorded with it (from the current span)."""
        ...

    async def get(self, command_id: UUID) -> Command | None: ...
    async def lock(self, command_id: UUID) -> Command | None: ...
    async def recent_for_device(self, device_id: str, *, limit: int) -> list[Command]: ...
    async def lock_overdue(self, before: datetime, *, limit: int) -> list[Command]: ...
    async def save(self, command: Command) -> None: ...


class AuditLog(Protocol):
    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None: ...


class Outbox(Protocol):
    async def add(self, message: OutboxMessage, *, created_at: datetime) -> None: ...


class CommandsUnitOfWork(Protocol):
    @property
    def commands(self) -> CommandRepository: ...
    @property
    def outbox(self) -> Outbox: ...
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


class CommandsUnitOfWorkFactory(Protocol):
    def __call__(self) -> CommandsUnitOfWork: ...


@dataclass(frozen=True, slots=True)
class Target:
    device_id: str
    home_id: UUID
    kind: DeviceKind
    accepts_commands: bool


class Devices(Protocol):
    """The devices module, as far as commands are concerned."""

    async def target(self, home_id: UUID, device_id: str) -> Target | None: ...
    async def set_desired(
        self, home_id: UUID, device_id: str, desired: dict[str, Any], *, at: datetime
    ) -> None: ...
