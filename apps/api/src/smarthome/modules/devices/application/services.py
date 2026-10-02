import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from device_protocol import DeviceKind
from smarthome.modules.devices.application.ports import (
    Broker,
    DevicesUnitOfWork,
    DevicesUnitOfWorkFactory,
    LiveState,
)
from smarthome.modules.devices.domain.errors import DeviceNotFound, InvalidPairingCode
from smarthome.modules.devices.domain.model import (
    Device,
    DeviceStatus,
    PairingCode,
    Twin,
    hash_code,
    new_device_id,
)
from smarthome.shared.audit import AuditEvent
from smarthome.shared.clock import Clock

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Claimed:
    device: Device
    mqtt_password: str


@dataclass(frozen=True, slots=True)
class DeviceView:
    device: Device
    twin: Twin


class DevicesService:
    def __init__(
        self, uow: DevicesUnitOfWorkFactory, broker: Broker, live: LiveState, clock: Clock
    ) -> None:
        self._uow = uow
        self._broker = broker
        self._live = live
        self._clock = clock

    # --- Pairing --------------------------------------------------------------------

    async def create_pairing_code(
        self,
        *,
        home_id: UUID,
        actor: str,
        name: str | None = None,
        room: str | None = None,
        kind: DeviceKind | None = None,
    ) -> tuple[PairingCode, str]:
        now = self._clock.now()
        code, plaintext = PairingCode.issue(
            home_id=home_id, created_by=actor, now=now, name=name, room=room, kind=kind
        )
        async with self._uow() as uow:
            await uow.pairing_codes.add(code)
            await uow.audit.append(
                AuditEvent(
                    actor=actor,
                    action="pairing_code.created",
                    target_type="pairing_code",
                    target_id=str(code.id),
                    tenant_id=home_id,
                    details={"kind": kind.value if kind else None, "name": name, "room": room},
                ),
                occurred_at=now,
            )
            await uow.commit()
        return code, plaintext

    async def claim(self, *, code: str, kind: DeviceKind, firmware: str | None) -> Claimed:
        """Exchange a pairing code for a device identity and its broker credentials.

        A saga across two systems: the broker credentials are created first, then the
        code is consumed and the device recorded in one transaction. If that
        transaction fails (expired code, a concurrent claim won), the broker
        credentials are removed again, so a failed claim never leaves a usable login.
        """
        now = self._clock.now()
        code_hash = hash_code(code)
        async with self._uow() as uow:
            candidate = await uow.pairing_codes.find(code_hash)
        if candidate is None:
            raise InvalidPairingCode("unknown, expired or already used pairing code")
        candidate.claim(kind=kind, now=now)  # cheap rejection before touching the broker

        device_id = new_device_id(kind)
        password = secrets.token_urlsafe(32)
        await self._broker.register_device(
            home_id=str(candidate.home_id), device_id=device_id, password=password
        )
        try:
            device = await self._record_claim(code_hash, device_id, kind, firmware)
        except BaseException:
            await self._broker.remove_device(device_id)
            raise
        log.info("device_paired", device_id=device_id, home_id=str(device.home_id), kind=kind.value)
        return Claimed(device=device, mqtt_password=password)

    async def _record_claim(
        self, code_hash: bytes, device_id: str, kind: DeviceKind, firmware: str | None
    ) -> Device:
        now = self._clock.now()
        async with self._uow() as uow:
            code = await uow.pairing_codes.lock(code_hash)
            if code is None:
                raise InvalidPairingCode("unknown, expired or already used pairing code")
            code.claim(kind=kind, now=now)
            device = Device(
                id=device_id,
                home_id=code.home_id,
                kind=kind,
                name=code.name or f"{kind.value.replace('_', ' ').title()} {device_id[-4:]}",
                room=code.room,
                firmware=firmware,
                status=DeviceStatus.ACTIVE,
                status_changed_at=now,
                paired_by=code.created_by,
                paired_at=now,
            )
            await uow.devices.add(device)
            await uow.twins.add(Twin(device_id=device_id, kind=kind))
            await uow.pairing_codes.mark_claimed(code.id, device_id=device_id, at=now)
            await uow.audit.append(
                AuditEvent(
                    actor=f"device:{device_id}",
                    action="device.paired",
                    target_type="device",
                    target_id=device_id,
                    tenant_id=code.home_id,
                    details={
                        "kind": kind.value,
                        "pairing_code_id": str(code.id),
                        "by": code.created_by,
                    },
                ),
                occurred_at=now,
            )
            await uow.commit()
        return device

    # --- Reads ----------------------------------------------------------------------

    async def list_devices(
        self, home_id: UUID, *, scope: frozenset[str] | None = None
    ) -> list[Device]:
        async with self._uow() as uow:
            devices = await uow.devices.for_home(home_id)
        return [d for d in devices if scope is None or d.id in scope]

    async def get(self, home_id: UUID, device_id: str) -> DeviceView:
        async with self._uow() as uow:
            device = await uow.devices.get(device_id)
            if device is None or device.home_id != home_id or device.status is DeviceStatus.REVOKED:
                raise DeviceNotFound(device_id)
            twin = await uow.twins.get(device_id)
        assert twin is not None  # created with the device  # noqa: S101
        return DeviceView(device, twin)

    async def is_active(self, home_id: UUID, device_id: str) -> bool:
        async with self._uow() as uow:
            device = await uow.devices.get(device_id)
        return device is not None and device.home_id == home_id and device.accepts_traffic

    # --- Lifecycle --------------------------------------------------------------------
    # Broker first, database second: if the second step fails, the device has already
    # lost (or regained) access, which is the safe side for quarantine and revocation.

    async def quarantine(self, home_id: UUID, device_id: str, *, actor: str, reason: str) -> Device:
        device = (await self.get(home_id, device_id)).device
        updated = device.quarantine(reason=reason, now=self._clock.now())
        await self._broker.disable_device(device_id)
        await self._save_transition(
            updated, actor=actor, action="device.quarantined", reason=reason
        )
        await self._live.set_presence(device_id, online=False, at=updated.status_changed_at)
        return updated

    async def release(self, home_id: UUID, device_id: str, *, actor: str) -> Device:
        device = (await self.get(home_id, device_id)).device
        updated = device.release(now=self._clock.now())
        await self._broker.enable_device(device_id)
        await self._save_transition(updated, actor=actor, action="device.released", reason=None)
        return updated

    async def revoke(self, home_id: UUID, device_id: str, *, actor: str, reason: str) -> None:
        device = (await self.get(home_id, device_id)).device
        updated = device.revoke(reason=reason, now=self._clock.now())
        await self._broker.remove_device(device_id)
        await self._save_transition(updated, actor=actor, action="device.revoked", reason=reason)
        await self._live.forget(device_id)

    async def _save_transition(
        self, device: Device, *, actor: str, action: str, reason: str | None
    ) -> None:
        async with self._uow() as uow:
            await uow.devices.save(device)
            await uow.audit.append(
                AuditEvent(
                    actor=actor,
                    action=action,
                    target_type="device",
                    target_id=device.id,
                    tenant_id=device.home_id,
                    details={"reason": reason, "kind": device.kind.value},
                ),
                occurred_at=device.status_changed_at,
            )
            await uow.commit()

    # --- Ingestion (called by the ingestor) ------------------------------------------

    async def record_presence(
        self, *, home_id: UUID, device_id: str, online: bool, at: datetime
    ) -> bool:
        """Returns False when the message is ignored (unknown, foreign, inactive or stale)."""
        async with self._uow() as uow:
            device = await self._active_device(uow, home_id, device_id)
            if device is None:
                return False
            updated = device.with_presence(online=online, at=at)
            if updated is None:
                return False
            await uow.devices.save(updated)
            await uow.commit()
        await self._live.set_presence(device_id, online=online, at=at)
        return True

    async def record_reported_state(
        self, *, home_id: UUID, device_id: str, reported: dict[str, Any], at: datetime
    ) -> bool:
        async with self._uow() as uow:
            if await self._active_device(uow, home_id, device_id) is None:
                return False
            twin = await uow.twins.lock(device_id)
            updated = twin.with_reported(reported, at=at) if twin else None
            if updated is None:
                return False
            await uow.twins.save_reported(updated)
            await uow.commit()
        await self._live.set_reported(device_id, reported, at=at)
        return True

    @staticmethod
    async def _active_device(
        uow: DevicesUnitOfWork, home_id: UUID, device_id: str
    ) -> Device | None:
        device = await uow.devices.lock(device_id)
        if device is None or device.home_id != home_id or not device.accepts_traffic:
            return None
        return device
