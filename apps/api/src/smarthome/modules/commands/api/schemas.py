from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from smarthome.modules.commands.domain.model import CommandAction, CommandStatus


class CommandCreate(BaseModel):
    action: CommandAction
    desired: dict[str, Any] | None = Field(
        default=None, description="Required for set_state; properties of the device's kind."
    )
    ttl_s: int = Field(
        default=30, ge=5, le=300, description="The device rejects the command after this."
    )


class CommandOut(BaseModel):
    id: UUID
    device_id: str
    action: CommandAction
    desired: dict[str, Any] | None
    status: CommandStatus
    issued_by: str
    issued_at: datetime
    expires_at: datetime
    delivered_at: datetime | None
    completed_at: datetime | None
    reason: str | None
    trace_id: str | None = Field(description="Look the command up in the tracing backend.")
