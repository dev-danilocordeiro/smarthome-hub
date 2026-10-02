"""Devices, how they are paired to a home, and their digital twin."""

import hashlib
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from device_protocol import STATE_PROPERTIES, DeviceKind
from smarthome.modules.devices.domain.errors import (
    InvalidPairingCode,
    InvalidTransition,
    UnsupportedState,
)

PAIRING_CODE_TTL = timedelta(minutes=10)
# Crockford base32: no I, L, O or U, so a code read aloud or off a screen is unambiguous.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8  # 40 bits; brute force is bounded by the claim rate limit and the TTL
_CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1"})


class DeviceStatus(StrEnum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    REVOKED = "revoked"


def new_device_id(kind: DeviceKind) -> str:
    prefix = kind.value.split("_")[0][:8]
    return f"{prefix}-{secrets.token_hex(6)}"


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    home_id: UUID
    kind: DeviceKind
    name: str
    room: str | None
    firmware: str | None
    status: DeviceStatus
    status_changed_at: datetime
    paired_by: str
    paired_at: datetime
    status_reason: str | None = None
    online: bool = False
    presence_changed_at: datetime | None = None
    last_seen_at: datetime | None = None

    @property
    def accepts_traffic(self) -> bool:
        return self.status is DeviceStatus.ACTIVE

    def quarantine(self, *, reason: str, now: datetime) -> "Device":
        if self.status is not DeviceStatus.ACTIVE:
            raise InvalidTransition(f"cannot quarantine a {self.status.value} device")
        return replace(
            self,
            status=DeviceStatus.QUARANTINED,
            status_reason=reason,
            status_changed_at=now,
            online=False,
        )

    def release(self, *, now: datetime) -> "Device":
        if self.status is not DeviceStatus.QUARANTINED:
            raise InvalidTransition(f"cannot release a {self.status.value} device")
        return replace(self, status=DeviceStatus.ACTIVE, status_reason=None, status_changed_at=now)

    def revoke(self, *, reason: str, now: datetime) -> "Device":
        if self.status is DeviceStatus.REVOKED:
            raise InvalidTransition("device is already revoked")
        return replace(
            self,
            status=DeviceStatus.REVOKED,
            status_reason=reason,
            status_changed_at=now,
            online=False,
        )

    def with_presence(self, *, online: bool, at: datetime) -> "Device | None":
        """Apply a presence change, or None if it is stale (MQTT may reorder across
        reconnects, and a late `offline` Will must not override a newer `online`)."""
        if self.presence_changed_at is not None and at < self.presence_changed_at:
            return None
        return replace(
            self,
            online=online,
            presence_changed_at=at,
            last_seen_at=max(at, self.last_seen_at) if self.last_seen_at else at,
        )


def normalize_code(raw: str) -> str:
    return raw.strip().upper().replace("-", "").replace(" ", "").translate(_CONFUSABLE)


def hash_code(raw: str) -> bytes:
    return hashlib.sha256(normalize_code(raw).encode()).digest()


@dataclass(frozen=True, slots=True)
class PairingCode:
    """A short, single-use code a resident types into (or shows to) a new device.

    Only the hash is stored. The plaintext is returned once, at creation.
    """

    id: UUID
    home_id: UUID
    code_hash: bytes
    created_by: str
    created_at: datetime
    expires_at: datetime
    name: str | None = None
    room: str | None = None
    kind: DeviceKind | None = None
    claimed_at: datetime | None = None
    claimed_device_id: str | None = None

    @staticmethod
    def issue(
        *,
        home_id: UUID,
        created_by: str,
        now: datetime,
        name: str | None = None,
        room: str | None = None,
        kind: DeviceKind | None = None,
    ) -> tuple["PairingCode", str]:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        code = PairingCode(
            id=uuid4(),
            home_id=home_id,
            code_hash=hash_code(raw),
            created_by=created_by,
            created_at=now,
            expires_at=now + PAIRING_CODE_TTL,
            name=name,
            room=room,
            kind=kind,
        )
        return code, f"{raw[:4]}-{raw[4:]}"

    def claim(self, *, kind: DeviceKind, now: datetime) -> None:
        """Raise unless this code can pair a device of `kind` right now."""
        if self.claimed_at is not None or now >= self.expires_at:
            raise InvalidPairingCode("unknown, expired or already used pairing code")
        if self.kind is not None and self.kind is not kind:
            raise InvalidPairingCode("this code was issued for a different kind of device")


@dataclass(frozen=True, slots=True)
class Twin:
    """Desired vs reported state. Commands change `desired`; the device changes
    `reported`. While they differ, the device has not caught up yet."""

    device_id: str
    kind: DeviceKind
    desired: dict[str, Any] = field(default_factory=dict)
    desired_at: datetime | None = None
    reported: dict[str, Any] = field(default_factory=dict)
    reported_at: datetime | None = None

    def with_reported(self, reported: dict[str, Any], *, at: datetime) -> "Twin | None":
        if self.reported_at is not None and at <= self.reported_at:
            return None  # stale or duplicate state message
        unknown = set(reported) - STATE_PROPERTIES[self.kind]
        if unknown:
            raise UnsupportedState(f"{self.kind.value} has no properties {sorted(unknown)}")
        return replace(self, reported=dict(reported), reported_at=at)

    def with_desired(self, desired: dict[str, Any], *, at: datetime) -> "Twin | None":
        """Merge what a command asks for into `desired`. Properties it does not mention
        keep their previous desired value. None if a newer command already wrote here."""
        if self.desired_at is not None and at <= self.desired_at:
            return None
        unknown = set(desired) - STATE_PROPERTIES[self.kind]
        if unknown:
            raise UnsupportedState(f"{self.kind.value} has no properties {sorted(unknown)}")
        return replace(self, desired={**self.desired, **desired}, desired_at=at)

    def delta(self) -> dict[str, Any]:
        """Desired properties the device has not reported yet."""
        return {k: v for k, v in self.desired.items() if self.reported.get(k) != v}

    @property
    def in_sync(self) -> bool:
        return not self.delta()
