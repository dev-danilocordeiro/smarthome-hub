from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from device_protocol import MessageKind, topic
from smarthome.modules.devices.api.container import DevicesModule
from smarthome.modules.devices.api.schemas import (
    ClaimOut,
    ClaimRequest,
    DeviceDetailOut,
    DeviceOut,
    MqttCredentials,
    PairingCodeCreate,
    PairingCodeOut,
    StatusChange,
    TwinOut,
)
from smarthome.modules.devices.domain.errors import DeviceNotFound, InvalidPairingCode
from smarthome.modules.devices.domain.model import Device
from smarthome.modules.identity.public import HomeAccess, Permission, require_home_access
from smarthome.shared.http.rate_limit import Limit

router = APIRouter(tags=["devices"])

# Pairing codes carry 40 bits and live 10 minutes; these limits keep guessing hopeless.
CLAIMS_PER_IP = Limit("claim-ip", max_hits=10, window_seconds=60)
CLAIM_FAILURES_PER_IP = Limit("claim-fail-ip", max_hits=20, window_seconds=3600)


class TooManyRequests(Exception):
    pass


def devices_module(request: Request) -> DevicesModule:
    module: DevicesModule = request.app.state.devices
    return module


Devices = Annotated[DevicesModule, Depends(devices_module)]
CanView = Annotated[HomeAccess, Depends(require_home_access(Permission.VIEW_HOME))]
CanManage = Annotated[HomeAccess, Depends(require_home_access(Permission.MANAGE_DEVICES))]


def _out(device: Device) -> DeviceOut:
    return DeviceOut(
        id=device.id,
        kind=device.kind,
        name=device.name,
        room=device.room,
        status=device.status.value,
        status_reason=device.status_reason,
        online=device.online,
        last_seen_at=device.last_seen_at,
        firmware=device.firmware,
        paired_at=device.paired_at,
    )


@router.post("/homes/{home_id}/pairing-codes", status_code=status.HTTP_201_CREATED)
async def create_pairing_code(
    body: PairingCodeCreate, access: CanManage, module: Devices
) -> PairingCodeOut:
    code, plaintext = await module.service.create_pairing_code(
        home_id=access.home.id,
        actor=access.membership.user_id,
        name=body.name,
        room=body.room,
        kind=body.kind,
    )
    return PairingCodeOut(code=plaintext, expires_at=code.expires_at)


@router.post(
    "/provisioning/claim",
    status_code=status.HTTP_201_CREATED,
    responses={
        410: {"description": "Unknown, expired or used code"},
        429: {"description": "Slow down"},
    },
)
async def claim(body: ClaimRequest, request: Request, module: Devices) -> ClaimOut:
    """Called by a device (no user session): trade a pairing code for an identity and
    broker credentials. The password is returned exactly once."""
    client_ip = request.client.host if request.client else "unknown"
    limiter = module.rate_limiter
    if not await limiter.hit(CLAIMS_PER_IP, client_ip) or await limiter.exceeded(
        CLAIM_FAILURES_PER_IP, client_ip
    ):
        raise TooManyRequests
    try:
        claimed = await module.service.claim(code=body.code, kind=body.kind, firmware=body.firmware)
    except InvalidPairingCode:
        await limiter.hit(CLAIM_FAILURES_PER_IP, client_ip)
        raise
    device = claimed.device
    settings = module.settings
    home = str(device.home_id)
    return ClaimOut(
        device_id=device.id,
        home_id=home,
        mqtt=MqttCredentials(
            host=settings.device_broker_host,
            port=settings.device_broker_port,
            username=device.id,
            client_id=device.id,
            password=claimed.mqtt_password,
        ),
        topics={kind.name.lower(): topic(home, device.id, kind) for kind in MessageKind},
    )


@router.get("/homes/{home_id}/devices")
async def list_devices(access: CanView, module: Devices) -> list[DeviceOut]:
    """Guests with a device scope only see the devices they were given."""
    devices = await module.service.list_devices(
        access.home.id, scope=access.membership.device_scope
    )
    return [_out(d) for d in devices]


@router.get("/homes/{home_id}/devices/{device_id}")
async def get_device(device_id: str, access: CanView, module: Devices) -> DeviceDetailOut:
    scope = access.membership.device_scope
    if scope is not None and device_id not in scope:
        raise DeviceNotFound(device_id)
    view = await module.service.get(access.home.id, device_id)
    twin = view.twin
    return DeviceDetailOut(
        **_out(view.device).model_dump(),
        twin=TwinOut(
            desired=twin.desired,
            desired_at=twin.desired_at,
            reported=twin.reported,
            reported_at=twin.reported_at,
            delta=twin.delta(),
            in_sync=twin.in_sync,
        ),
    )


@router.post("/homes/{home_id}/devices/{device_id}/quarantine")
async def quarantine(
    device_id: str, body: StatusChange, access: CanManage, module: Devices
) -> DeviceOut:
    """Cut the device off the broker now (it is disconnected immediately); reversible."""
    device = await module.service.quarantine(
        access.home.id, device_id, actor=access.membership.user_id, reason=body.reason
    )
    return _out(device)


@router.post("/homes/{home_id}/devices/{device_id}/release")
async def release(device_id: str, access: CanManage, module: Devices) -> DeviceOut:
    device = await module.service.release(
        access.home.id, device_id, actor=access.membership.user_id
    )
    return _out(device)


@router.delete("/homes/{home_id}/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(device_id: str, access: CanManage, module: Devices) -> Response:
    """Unpair: credentials are deleted at the broker and the device is kicked off."""
    await module.service.revoke(
        access.home.id, device_id, actor=access.membership.user_id, reason="unpaired by a member"
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
