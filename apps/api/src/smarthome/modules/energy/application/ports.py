from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from smarthome.modules.energy.domain.tariff import Tariff
from smarthome.modules.energy.domain.usage import HourlyUsage
from smarthome.shared.audit import AuditEvent


@dataclass(frozen=True, slots=True)
class Increase:
    """A counter's growth in one hour, as telemetry reports it."""

    home_id: UUID
    device_id: str
    hour: datetime
    wh: float


@dataclass(frozen=True, slots=True)
class MeteredDevice:
    id: str
    name: str | None
    room: str | None
    whole_home: bool


class HourlyStore(Protocol):
    async def upsert(self, rows: list[Increase]) -> int: ...
    async def for_home(
        self, home_id: UUID, *, start: datetime, end: datetime
    ) -> list[HourlyUsage]: ...


class RollupCursor(Protocol):
    async def try_lock(self) -> bool:
        """Take the rollup lock until the transaction ends; False if another worker has it."""
        ...

    async def get(self) -> datetime | None: ...
    async def advance(self, until: datetime) -> None: ...


class Tariffs(Protocol):
    async def get(self, home_id: UUID) -> Tariff | None: ...
    async def lock(self, home_id: UUID) -> Tariff | None: ...
    async def save(self, tariff: Tariff) -> None: ...
    async def with_budget(self) -> list[Tariff]: ...


class AuditLog(Protocol):
    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None: ...


class EnergyUnitOfWork(Protocol):
    @property
    def hourly(self) -> HourlyStore: ...
    @property
    def cursor(self) -> RollupCursor: ...
    @property
    def tariffs(self) -> Tariffs: ...
    @property
    def audit(self) -> AuditLog: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...


class EnergyUnitOfWorkFactory(Protocol):
    def __call__(self) -> EnergyUnitOfWork: ...


class CounterSource(Protocol):
    async def hourly_increase(self, *, start: datetime, end: datetime) -> list[Increase]: ...


class DeviceDirectory(Protocol):
    async def metered(self, home_id: UUID) -> list[MeteredDevice]:
        """Devices that report an energy counter: plugs and whole-home meters."""
        ...
