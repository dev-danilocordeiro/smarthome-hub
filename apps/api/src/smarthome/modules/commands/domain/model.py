"""Commands sent to devices and how their outcome is tracked."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from device_protocol import (
    CRITICAL_KINDS,
    STATE_PROPERTIES,
    DeviceKind,
    InvalidMessage,
    MessageKind,
    validate,
)
from smarthome.modules.commands.domain.errors import InvalidCommand

DEFAULT_TTL = timedelta(seconds=30)
MIN_TTL = timedelta(seconds=5)
MAX_TTL = timedelta(minutes=5)
# Commands to locks and cameras need a sign-in this recent (step-up authentication).
RECENT_AUTHENTICATION = timedelta(minutes=5)


class CommandAction(StrEnum):
    SET_STATE = "set_state"
    IDENTIFY = "identify"
    REBOOT = "reboot"


class CommandStatus(StrEnum):
    PENDING = "pending"  # queued in the outbox or on its way to the device
    DELIVERED = "delivered"  # the device confirmed receipt
    ACKNOWLEDGED = "acknowledged"  # the device applied it
    FAILED = "failed"  # the device rejected it or could not apply it
    TIMED_OUT = "timed_out"  # no outcome before the deadline (or it arrived too late)


SETTLED = frozenset({CommandStatus.ACKNOWLEDGED, CommandStatus.FAILED, CommandStatus.TIMED_OUT})


class AckStatus(StrEnum):
    """What a device reports in `command_ack` (device protocol v1)."""

    RECEIVED = "received"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED = "expired"


class Capability(StrEnum):
    """What a caller must be allowed to do to send a command. The API maps these to
    identity permissions; the domain only decides which ones a command needs."""

    CONTROL = "control"  # everyday devices
    OPERATE_SECURITY = "operate_security"  # locks and cameras
    MAINTAIN = "maintain"  # reboot


@dataclass(frozen=True, slots=True)
class Requirements:
    capabilities: frozenset[Capability]
    recent_authentication: timedelta | None


def requirements(kind: DeviceKind, action: CommandAction) -> Requirements:
    capabilities = {Capability.CONTROL}
    recent = None
    if kind in CRITICAL_KINDS:
        capabilities.add(Capability.OPERATE_SECURITY)
        recent = RECENT_AUTHENTICATION
    if action is CommandAction.REBOOT:
        capabilities.add(Capability.MAINTAIN)
    return Requirements(frozenset(capabilities), recent)


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Command:
    id: UUID
    home_id: UUID
    device_id: str
    device_kind: DeviceKind
    action: CommandAction
    desired: dict[str, Any] | None
    status: CommandStatus
    issued_by: str
    issued_at: datetime
    expires_at: datetime
    delivered_at: datetime | None = None
    completed_at: datetime | None = None
    reason: str | None = None
    trace_id: str | None = None

    @staticmethod
    def issue(
        *,
        home_id: UUID,
        device_id: str,
        device_kind: DeviceKind,
        action: CommandAction,
        desired: dict[str, Any] | None,
        issued_by: str,
        now: datetime,
        ttl: timedelta = DEFAULT_TTL,
    ) -> "Command":
        if not MIN_TTL <= ttl <= MAX_TTL:
            raise InvalidCommand(f"ttl must be between {MIN_TTL} and {MAX_TTL}")
        if action is CommandAction.SET_STATE:
            if not desired:
                raise InvalidCommand("set_state needs at least one desired property")
            unknown = set(desired) - STATE_PROPERTIES[device_kind]
            if unknown:
                raise InvalidCommand(f"{device_kind.value} has no properties {sorted(unknown)}")
        elif desired is not None:
            raise InvalidCommand(f"{action.value} takes no desired state")
        command = Command(
            id=uuid4(),
            home_id=home_id,
            device_id=device_id,
            device_kind=device_kind,
            action=action,
            desired=dict(desired) if desired is not None else None,
            status=CommandStatus.PENDING,
            issued_by=issued_by,
            issued_at=now,
            expires_at=now + ttl,
        )
        try:  # value ranges and types come from the protocol's JSON Schema
            validate(MessageKind.COMMAND, command.message())
        except InvalidMessage as exc:
            raise InvalidCommand(str(exc)) from exc
        return command

    @property
    def critical(self) -> bool:
        return self.device_kind in CRITICAL_KINDS

    @property
    def settled(self) -> bool:
        return self.status in SETTLED

    def message(self) -> dict[str, Any]:
        """The `command` payload of device protocol v1."""
        payload: dict[str, Any] = {
            "schema_version": "1",
            "command_id": str(self.id),
            "issued_at": _iso(self.issued_at),
            "expires_at": _iso(self.expires_at),
            "action": self.action.value,
        }
        if self.desired is not None:
            payload["desired"] = self.desired
        return payload

    def with_ack(self, ack: AckStatus, *, at: datetime, reason: str | None) -> "Command | None":
        """Apply a device ack, or None if it changes nothing.

        Acks travel at QoS 1 and may be duplicated or arrive out of order ("applied"
        before "received"). Status only moves forward, and a settled command stays
        settled: an outcome that arrives after the timeout is not rewritten.
        """
        if self.settled:
            return None
        if ack is AckStatus.RECEIVED:
            if self.status is CommandStatus.DELIVERED:
                return None
            return replace(self, status=CommandStatus.DELIVERED, delivered_at=at)
        delivered_at = self.delivered_at or at
        if ack is AckStatus.APPLIED:
            status, why = CommandStatus.ACKNOWLEDGED, None
        elif ack is AckStatus.EXPIRED:
            status, why = CommandStatus.TIMED_OUT, reason or "expired before the device got it"
        else:
            status, why = CommandStatus.FAILED, f"{ack.value}: {reason}" if reason else ack.value
        return replace(self, status=status, delivered_at=delivered_at, completed_at=at, reason=why)

    def time_out(self, *, now: datetime, grace: timedelta) -> "Command | None":
        """Settle as timed out once the deadline (plus time for the ack) has passed."""
        if self.settled or now < self.expires_at + grace:
            return None
        return replace(
            self, status=CommandStatus.TIMED_OUT, completed_at=now, reason="no outcome in time"
        )
