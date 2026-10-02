"""Postgres adapters for the devices ports (schema `devices`)."""

import json
from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from device_protocol import DeviceKind
from smarthome.modules.devices.domain.model import Device, DeviceStatus, PairingCode, Twin
from smarthome.shared.infrastructure.audit import PostgresAuditLog

SELECT_DEVICE = text(
    "SELECT id, home_id, kind, name, room, firmware, status, status_reason, status_changed_at,"
    " paired_by, paired_at, online, presence_changed_at, last_seen_at"
    " FROM devices.devices WHERE id = :id"
)
LOCK_DEVICE = text(
    "SELECT id, home_id, kind, name, room, firmware, status, status_reason, status_changed_at,"
    " paired_by, paired_at, online, presence_changed_at, last_seen_at"
    " FROM devices.devices WHERE id = :id FOR UPDATE"
)
SELECT_HOME_DEVICES = text(
    "SELECT id, home_id, kind, name, room, firmware, status, status_reason, status_changed_at,"
    " paired_by, paired_at, online, presence_changed_at, last_seen_at"
    " FROM devices.devices"
    " WHERE home_id = :home AND status <> 'revoked' ORDER BY room NULLS LAST, name"
)
SELECT_CODE = text(
    "SELECT id, home_id, code_hash, created_by, created_at, expires_at, name, room, kind,"
    " claimed_at, claimed_device_id FROM devices.pairing_codes WHERE code_hash = :h"
)
LOCK_CODE = text(
    "SELECT id, home_id, code_hash, created_by, created_at, expires_at, name, room, kind,"
    " claimed_at, claimed_device_id FROM devices.pairing_codes WHERE code_hash = :h FOR UPDATE"
)
SELECT_TWIN = text(
    "SELECT t.device_id, d.kind, t.desired, t.desired_at, t.reported, t.reported_at"
    " FROM devices.twins t JOIN devices.devices d ON d.id = t.device_id WHERE t.device_id = :id"
)
LOCK_TWIN = text(
    "SELECT t.device_id, d.kind, t.desired, t.desired_at, t.reported, t.reported_at"
    " FROM devices.twins t JOIN devices.devices d ON d.id = t.device_id WHERE t.device_id = :id"
    " FOR UPDATE OF t"
)


def _device(row: Row[Any]) -> Device:
    return Device(
        id=row.id,
        home_id=row.home_id,
        kind=DeviceKind(row.kind),
        name=row.name,
        room=row.room,
        firmware=row.firmware,
        status=DeviceStatus(row.status),
        status_reason=row.status_reason,
        status_changed_at=row.status_changed_at,
        paired_by=row.paired_by,
        paired_at=row.paired_at,
        online=row.online,
        presence_changed_at=row.presence_changed_at,
        last_seen_at=row.last_seen_at,
    )


def _code(row: Row[Any]) -> PairingCode:
    return PairingCode(
        id=row.id,
        home_id=row.home_id,
        code_hash=bytes(row.code_hash),
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        name=row.name,
        room=row.room,
        kind=DeviceKind(row.kind) if row.kind else None,
        claimed_at=row.claimed_at,
        claimed_device_id=row.claimed_device_id,
    )


def _twin(row: Row[Any]) -> Twin:
    return Twin(
        device_id=row.device_id,
        kind=DeviceKind(row.kind),
        desired=row.desired,
        desired_at=row.desired_at,
        reported=row.reported,
        reported_at=row.reported_at,
    )


class PostgresDevices:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, device: Device) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO devices.devices (id, home_id, kind, name, room, firmware, status,"
                " status_reason, status_changed_at, paired_by, paired_at)"
                " VALUES (:id, :home, :kind, :name, :room, :fw, :status, :reason, :changed,"
                " :by, :at)"
            ),
            {
                "id": device.id,
                "home": device.home_id,
                "kind": device.kind.value,
                "name": device.name,
                "room": device.room,
                "fw": device.firmware,
                "status": device.status.value,
                "reason": device.status_reason,
                "changed": device.status_changed_at,
                "by": device.paired_by,
                "at": device.paired_at,
            },
        )

    async def get(self, device_id: str) -> Device | None:
        row = (await self._conn.execute(SELECT_DEVICE, {"id": device_id})).first()
        return _device(row) if row else None

    async def lock(self, device_id: str) -> Device | None:
        row = (await self._conn.execute(LOCK_DEVICE, {"id": device_id})).first()
        return _device(row) if row else None

    async def for_home(self, home_id: UUID) -> list[Device]:
        return [
            _device(r) for r in await self._conn.execute(SELECT_HOME_DEVICES, {"home": home_id})
        ]

    async def save(self, device: Device) -> None:
        await self._conn.execute(
            text(
                "UPDATE devices.devices SET status = :status, status_reason = :reason,"
                " status_changed_at = :changed, online = :online,"
                " presence_changed_at = :presence_at, last_seen_at = :seen WHERE id = :id"
            ),
            {
                "id": device.id,
                "status": device.status.value,
                "reason": device.status_reason,
                "changed": device.status_changed_at,
                "online": device.online,
                "presence_at": device.presence_changed_at,
                "seen": device.last_seen_at,
            },
        )


class PostgresPairingCodes:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, code: PairingCode) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO devices.pairing_codes (id, home_id, code_hash, created_by,"
                " created_at, expires_at, name, room, kind)"
                " VALUES (:id, :home, :hash, :by, :at, :expires, :name, :room, :kind)"
            ),
            {
                "id": code.id,
                "home": code.home_id,
                "hash": code.code_hash,
                "by": code.created_by,
                "at": code.created_at,
                "expires": code.expires_at,
                "name": code.name,
                "room": code.room,
                "kind": code.kind.value if code.kind else None,
            },
        )

    async def find(self, code_hash: bytes) -> PairingCode | None:
        row = (await self._conn.execute(SELECT_CODE, {"h": code_hash})).first()
        return _code(row) if row else None

    async def lock(self, code_hash: bytes) -> PairingCode | None:
        row = (await self._conn.execute(LOCK_CODE, {"h": code_hash})).first()
        return _code(row) if row else None

    async def mark_claimed(self, code_id: UUID, *, device_id: str, at: datetime) -> None:
        result = await self._conn.execute(
            text(
                "UPDATE devices.pairing_codes SET claimed_at = :at, claimed_device_id = :device"
                " WHERE id = :id AND claimed_at IS NULL"
            ),
            {"id": code_id, "at": at, "device": device_id},
        )
        if result.rowcount != 1:
            raise RuntimeError(f"pairing code {code_id} was claimed concurrently")


class PostgresTwins:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, twin: Twin) -> None:
        await self._conn.execute(
            text("INSERT INTO devices.twins (device_id) VALUES (:id)"), {"id": twin.device_id}
        )

    async def get(self, device_id: str) -> Twin | None:
        row = (await self._conn.execute(SELECT_TWIN, {"id": device_id})).first()
        return _twin(row) if row else None

    async def lock(self, device_id: str) -> Twin | None:
        row = (await self._conn.execute(LOCK_TWIN, {"id": device_id})).first()
        return _twin(row) if row else None

    async def save_reported(self, twin: Twin) -> None:
        await self._conn.execute(
            text(
                "UPDATE devices.twins SET reported = CAST(:reported AS jsonb), reported_at = :at"
                " WHERE device_id = :id"
            ),
            {"id": twin.device_id, "reported": json.dumps(twin.reported), "at": twin.reported_at},
        )

    async def save_desired(self, twin: Twin) -> None:
        await self._conn.execute(
            text(
                "UPDATE devices.twins SET desired = CAST(:desired AS jsonb), desired_at = :at"
                " WHERE device_id = :id"
            ),
            {"id": twin.device_id, "desired": json.dumps(twin.desired), "at": twin.desired_at},
        )


class PostgresDevicesUnitOfWork:
    devices: PostgresDevices
    pairing_codes: PostgresPairingCodes
    twins: PostgresTwins
    audit: PostgresAuditLog

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn: AsyncConnection | None = None
        self._tx: AsyncTransaction | None = None

    async def __aenter__(self) -> Self:
        self._conn = await self._engine.connect()
        self._tx = await self._conn.begin()
        self.devices = PostgresDevices(self._conn)
        self.pairing_codes = PostgresPairingCodes(self._conn)
        self.twins = PostgresTwins(self._conn)
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
