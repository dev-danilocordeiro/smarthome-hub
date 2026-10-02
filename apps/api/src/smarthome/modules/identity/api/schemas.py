from datetime import datetime
from typing import Annotated, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from smarthome.modules.identity.domain.model import HOME_NAME_MAX_LENGTH, Role

DeviceId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]


class UserOut(BaseModel):
    id: str
    email: str | None
    name: str | None


class SessionOut(BaseModel):
    user: UserOut
    authenticated_at: datetime
    csrf_token: str = Field(description="Send back as X-CSRF-Token on every unsafe request.")


class LogoutOut(BaseModel):
    logout_url: str


class HomeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=HOME_NAME_MAX_LENGTH)
    timezone: str = "America/Sao_Paulo"

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value


class HomeOut(BaseModel):
    id: UUID
    name: str
    timezone: str
    role: Role
    access_expires_at: datetime | None = None
    device_scope: list[str] | None = None


class MemberOut(BaseModel):
    user_id: str
    name: str | None
    role: Role
    granted_at: datetime
    expires_at: datetime | None
    device_scope: list[str] | None


class InvitationCreate(BaseModel):
    role: Role
    guest_access_expires_at: datetime | None = None
    device_scope: list[DeviceId] | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def guest_fields_only_for_guests(self) -> Self:
        if self.role is Role.OWNER:
            raise ValueError("ownership cannot be granted through an invitation")
        if self.role is Role.GUEST and self.guest_access_expires_at is None:
            raise ValueError("guest invitations need guest_access_expires_at")
        if self.role is not Role.GUEST and (self.guest_access_expires_at or self.device_scope):
            raise ValueError("only guest invitations carry an expiry or a device scope")
        if self.guest_access_expires_at and self.guest_access_expires_at.tzinfo is None:
            raise ValueError("guest_access_expires_at must include a timezone offset")
        return self


class InvitationOut(BaseModel):
    id: UUID
    role: Role
    expires_at: datetime
    token: str = Field(description="Shown once. Share it with the invitee; it is single use.")


class InvitationAccept(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class AuditEntryOut(BaseModel):
    id: int
    occurred_at: datetime
    actor: str
    action: str
    target_type: str
    target_id: str
    details: dict[str, object]
    trace_id: str | None


class AuditLogOut(BaseModel):
    chain_intact: bool = Field(description="False if any stored entry fails hash verification.")
    entries: list[AuditEntryOut]
