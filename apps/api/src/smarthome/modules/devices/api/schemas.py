from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from device_protocol import DeviceKind


class PairingCodeCreate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    room: str | None = Field(default=None, min_length=1, max_length=40)
    kind: DeviceKind | None = Field(default=None, description="Restrict the code to one kind.")


class PairingCodeOut(BaseModel):
    code: str = Field(description="Shown once. Enter it on the device within 10 minutes.")
    expires_at: datetime


class ClaimRequest(BaseModel):
    code: str = Field(min_length=8, max_length=16)
    kind: DeviceKind
    firmware: str | None = Field(default=None, max_length=40, pattern=r"^[\w.+-]+$")


class MqttCredentials(BaseModel):
    host: str
    port: int
    tls: bool = True
    username: str
    client_id: str
    password: str = Field(description="Returned once; the hub stores it only inside the broker.")


class ClaimOut(BaseModel):
    device_id: str
    home_id: str
    mqtt: MqttCredentials
    topics: dict[str, str]


class DeviceOut(BaseModel):
    id: str
    kind: DeviceKind
    name: str
    room: str | None
    status: str
    status_reason: str | None
    online: bool
    last_seen_at: datetime | None
    firmware: str | None
    paired_at: datetime


class TwinOut(BaseModel):
    desired: dict[str, Any]
    desired_at: datetime | None
    reported: dict[str, Any]
    reported_at: datetime | None
    delta: dict[str, Any] = Field(description="Desired values the device has not reported yet.")
    in_sync: bool


class DeviceDetailOut(DeviceOut):
    twin: TwinOut


class StatusChange(BaseModel):
    reason: str = Field(min_length=3, max_length=200)
