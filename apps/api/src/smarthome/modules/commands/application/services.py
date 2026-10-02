from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import structlog
from opentelemetry import metrics

from device_protocol import DELIVERY, MessageKind, topic
from smarthome.modules.commands.application.ports import (
    CommandsUnitOfWork,
    CommandsUnitOfWorkFactory,
    Devices,
    Target,
)
from smarthome.modules.commands.domain.errors import (
    CommandNotFound,
    TargetNotFound,
    TargetUnavailable,
)
from smarthome.modules.commands.domain.model import (
    DEFAULT_TTL,
    AckStatus,
    Command,
    CommandAction,
    Requirements,
    requirements,
)
from smarthome.shared.audit import AuditEvent
from smarthome.shared.clock import Clock
from smarthome.shared.outbox import OutboxMessage

log = structlog.get_logger(__name__)
meter = metrics.get_meter(__name__)

issued_counter = meter.create_counter(
    "smarthome.commands.issued",
    unit="{command}",
    description="Commands accepted for delivery, by device kind and action.",
)
settled_counter = meter.create_counter(
    "smarthome.commands.settled",
    unit="{command}",
    description="Commands that reached a final status, by status and action.",
)
outcome_latency = meter.create_histogram(
    "smarthome.commands.outcome.latency",
    unit="s",
    description="Time from issuing a command to its final status, by status and action.",
)

SYSTEM_ACTOR = "system:commands"
SWEEP_BATCH = 500


class CommandsService:
    def __init__(
        self,
        uow: CommandsUnitOfWorkFactory,
        devices: Devices,
        clock: Clock,
        *,
        ack_grace: timedelta = timedelta(seconds=10),
    ) -> None:
        self._uow = uow
        self._devices = devices
        self._clock = clock
        self._ack_grace = ack_grace

    async def target(self, home_id: UUID, device_id: str) -> Target:
        target = await self._devices.target(home_id, device_id)
        if target is None:
            raise TargetNotFound(device_id)
        return target

    async def requirements(
        self, home_id: UUID, device_id: str, action: CommandAction
    ) -> Requirements:
        """What the caller must be allowed (checked by the API before `issue`)."""
        return requirements((await self.target(home_id, device_id)).kind, action)

    async def issue(
        self,
        *,
        home_id: UUID,
        device_id: str,
        action: CommandAction,
        desired: dict[str, Any] | None,
        actor: str,
        ttl: timedelta = DEFAULT_TTL,
    ) -> Command:
        """Record the command and queue its MQTT message in one transaction (outbox).

        Nothing is published here: the worker relays the outbox after commit, so a
        command is sent if and only if it was recorded.
        """
        target = await self.target(home_id, device_id)
        if not target.accepts_commands:
            raise TargetUnavailable(f"device {device_id} is not active")
        now = self._clock.now()
        command = Command.issue(
            home_id=home_id,
            device_id=device_id,
            device_kind=target.kind,
            action=action,
            desired=desired,
            issued_by=actor,
            now=now,
            ttl=ttl,
        )
        async with self._uow() as uow:
            trace_id = await uow.commands.add(command)
            await uow.outbox.add(
                OutboxMessage(
                    topic=topic(str(home_id), device_id, MessageKind.COMMAND),
                    payload=command.message(),
                    qos=DELIVERY[MessageKind.COMMAND].qos,
                    expires_at=command.expires_at,
                ),
                created_at=now,
            )
            if command.critical:
                await uow.audit.append(self._audit(command, "command.issued"), occurred_at=now)
            await uow.commit()
        issued_counter.add(1, {"kind": target.kind.value, "action": action.value})
        log.info(
            "command_issued",
            command_id=str(command.id),
            device_id=device_id,
            action=action.value,
        )
        if command.desired is not None:
            await self._record_desired(command)
        return replace(command, trace_id=trace_id)

    async def _record_desired(self, command: Command) -> None:
        # The twin belongs to the devices module, so this is a second transaction. If it
        # fails the command still goes out; the twin catches up with the next command.
        assert command.desired is not None  # noqa: S101 - set_state only
        try:
            await self._devices.set_desired(
                command.home_id, command.device_id, command.desired, at=command.issued_at
            )
        except Exception:
            log.warning("twin_desired_not_recorded", command_id=str(command.id), exc_info=True)

    async def get(self, home_id: UUID, device_id: str, command_id: UUID) -> Command:
        async with self._uow() as uow:
            command = await uow.commands.get(command_id)
        if command is None or command.home_id != home_id or command.device_id != device_id:
            raise CommandNotFound(str(command_id))
        return command

    async def recent(self, home_id: UUID, device_id: str, *, limit: int = 20) -> list[Command]:
        await self.target(home_id, device_id)
        async with self._uow() as uow:
            commands = await uow.commands.recent_for_device(device_id, limit=limit)
        return [c for c in commands if c.home_id == home_id]

    # --- Outcomes ---------------------------------------------------------------------

    async def record_ack(
        self,
        *,
        home_id: UUID,
        device_id: str,
        command_id: UUID,
        ack: AckStatus,
        reason: str | None,
    ) -> bool:
        """Apply a device's ack (called by the ingestor). False if it changed nothing:
        unknown command, sent by another device, duplicate, or already settled.

        Ordered by the hub's clock: device clocks drift, and latency is the hub's view.
        """
        now = self._clock.now()
        async with self._uow() as uow:
            command = await uow.commands.lock(command_id)
            if command is None or command.home_id != home_id or command.device_id != device_id:
                return False
            updated = command.with_ack(ack, at=now, reason=reason)
            if updated is None:
                return False
            await uow.commands.save(updated)
            await self._settled(uow, updated)
            await uow.commit()
        return True

    async def expire_overdue(self) -> int:
        """Settle commands nobody answered in time (run periodically by the worker)."""
        now = self._clock.now()
        async with self._uow() as uow:
            overdue = await uow.commands.lock_overdue(now - self._ack_grace, limit=SWEEP_BATCH)
            expired = [
                c for c in (o.time_out(now=now, grace=self._ack_grace) for o in overdue) if c
            ]
            for command in expired:
                await uow.commands.save(command)
                await self._settled(uow, command)
            await uow.commit()
        if expired:
            log.info("commands_timed_out", count=len(expired))
        return len(expired)

    async def _settled(self, uow: CommandsUnitOfWork, command: Command) -> None:
        if not command.settled or command.completed_at is None:
            return
        attributes = {"status": command.status.value, "action": command.action.value}
        settled_counter.add(1, attributes)
        outcome_latency.record(
            (command.completed_at - command.issued_at).total_seconds(), attributes
        )
        if command.critical:
            await uow.audit.append(
                self._audit(command, f"command.{command.status.value}", actor=SYSTEM_ACTOR),
                occurred_at=command.completed_at,
            )

    @staticmethod
    def _audit(command: Command, action: str, *, actor: str | None = None) -> AuditEvent:
        return AuditEvent(
            actor=actor or command.issued_by,
            action=action,
            target_type="device",
            target_id=command.device_id,
            tenant_id=command.home_id,
            details={
                "command_id": str(command.id),
                "action": command.action.value,
                "desired": command.desired,
                "reason": command.reason,
            },
        )
