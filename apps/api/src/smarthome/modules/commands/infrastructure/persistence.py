"""Postgres adapters for the commands ports (schema `commands`, plus the shared outbox)."""

import json
from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from device_protocol import DeviceKind
from smarthome.modules.commands.domain.model import Command, CommandAction, CommandStatus
from smarthome.shared.infrastructure.audit import PostgresAuditLog
from smarthome.shared.infrastructure.outbox import PostgresOutbox

COLUMNS = (
    "id, home_id, device_id, device_kind, action, desired, status, issued_by, issued_at,"
    " expires_at, delivered_at, completed_at, reason, trace_id"
)
SELECT_COMMAND = text(f"SELECT {COLUMNS} FROM commands.commands WHERE id = :id")  # noqa: S608
LOCK_COMMAND = text(f"SELECT {COLUMNS} FROM commands.commands WHERE id = :id FOR UPDATE")  # noqa: S608
SELECT_RECENT = text(
    f"SELECT {COLUMNS} FROM commands.commands WHERE device_id = :device"  # noqa: S608
    " ORDER BY issued_at DESC LIMIT :limit"
)
# SKIP LOCKED: a command whose ack is being applied right now is left to that ack.
LOCK_OVERDUE = text(
    f"SELECT {COLUMNS} FROM commands.commands"  # noqa: S608
    " WHERE status IN ('pending', 'delivered') AND expires_at < :before"
    " ORDER BY expires_at LIMIT :limit FOR UPDATE SKIP LOCKED"
)


def _command(row: Row[Any]) -> Command:
    return Command(
        id=row.id,
        home_id=row.home_id,
        device_id=row.device_id,
        device_kind=DeviceKind(row.device_kind),
        action=CommandAction(row.action),
        desired=row.desired,
        status=CommandStatus(row.status),
        issued_by=row.issued_by,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        delivered_at=row.delivered_at,
        completed_at=row.completed_at,
        reason=row.reason,
        trace_id=row.trace_id,
    )


class PostgresCommands:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, command: Command) -> str | None:
        span_context = trace.get_current_span().get_span_context()
        trace_id = format(span_context.trace_id, "032x") if span_context.is_valid else None
        await self._conn.execute(
            text(
                f"INSERT INTO commands.commands ({COLUMNS})"  # noqa: S608
                " VALUES (:id, :home, :device, :kind, :action, CAST(:desired AS jsonb), :status,"
                " :by, :issued_at, :expires_at, :delivered_at, :completed_at, :reason, :trace)"
            ),
            {
                "id": command.id,
                "home": command.home_id,
                "device": command.device_id,
                "kind": command.device_kind.value,
                "action": command.action.value,
                "desired": json.dumps(command.desired) if command.desired is not None else None,
                "status": command.status.value,
                "by": command.issued_by,
                "issued_at": command.issued_at,
                "expires_at": command.expires_at,
                "delivered_at": command.delivered_at,
                "completed_at": command.completed_at,
                "reason": command.reason,
                "trace": trace_id,
            },
        )
        return trace_id

    async def get(self, command_id: UUID) -> Command | None:
        row = (await self._conn.execute(SELECT_COMMAND, {"id": command_id})).first()
        return _command(row) if row else None

    async def lock(self, command_id: UUID) -> Command | None:
        row = (await self._conn.execute(LOCK_COMMAND, {"id": command_id})).first()
        return _command(row) if row else None

    async def recent_for_device(self, device_id: str, *, limit: int) -> list[Command]:
        rows = await self._conn.execute(SELECT_RECENT, {"device": device_id, "limit": limit})
        return [_command(r) for r in rows]

    async def lock_overdue(self, before: datetime, *, limit: int) -> list[Command]:
        rows = await self._conn.execute(LOCK_OVERDUE, {"before": before, "limit": limit})
        return [_command(r) for r in rows]

    async def save(self, command: Command) -> None:
        await self._conn.execute(
            text(
                "UPDATE commands.commands SET status = :status, delivered_at = :delivered_at,"
                " completed_at = :completed_at, reason = :reason WHERE id = :id"
            ),
            {
                "id": command.id,
                "status": command.status.value,
                "delivered_at": command.delivered_at,
                "completed_at": command.completed_at,
                "reason": command.reason,
            },
        )


class PostgresCommandsUnitOfWork:
    commands: PostgresCommands
    outbox: PostgresOutbox
    audit: PostgresAuditLog

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn: AsyncConnection | None = None
        self._tx: AsyncTransaction | None = None

    async def __aenter__(self) -> Self:
        self._conn = await self._engine.connect()
        self._tx = await self._conn.begin()
        self.commands = PostgresCommands(self._conn)
        self.outbox = PostgresOutbox(self._conn)
        self.audit = PostgresAuditLog(self._conn)
        return self

    async def commit(self) -> None:
        if self._tx is None:
            raise RuntimeError("unit of work is not active")
        await self._tx.commit()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._tx is not None and self._tx.is_active:
                await self._tx.rollback()
        finally:
            if self._conn is not None:
                await self._conn.close()
            self._conn = None
            self._tx = None
