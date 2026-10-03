from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from smarthome.modules.automations.domain.dry_run import Outcome
from smarthome.modules.automations.domain.model import AutomationStatus, RunStatus


class AutomationIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool = True
    definition: dict[str, Any] = Field(
        description="Automation DSL v1; see GET /automations/schema."
    )


class AutomationOut(BaseModel):
    id: UUID
    name: str
    description: str | None
    enabled: bool
    status: AutomationStatus
    status_reason: str | None
    version: int
    definition: dict[str, Any]
    timezone: str
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    warnings: list[str] = Field(
        default_factory=list, description="Possible loops with other automations (on save)."
    )


class RevisionOut(BaseModel):
    version: int
    name: str
    definition: dict[str, Any]
    change: str
    changed_by: str
    changed_at: datetime


class ActionOutcomeOut(BaseModel):
    device_id: str
    command_id: UUID | None
    error: str | None


class RunOut(BaseModel):
    id: UUID
    automation_version: int
    trigger_index: int
    status: RunStatus
    reason: str | None
    depth: int
    started_at: datetime
    finished_at: datetime | None
    outcomes: list[ActionOutcomeOut]
    trace_id: str | None


class DryRunIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    definition: dict[str, Any]
    start: datetime = Field(alias="from")
    end: datetime | None = Field(default=None, alias="to", description="Defaults to now.")


class SimulatedRunOut(BaseModel):
    at: datetime
    trigger_index: int
    outcome: Outcome
    reason: str | None
    failed_conditions: list[int]
    unknown_conditions: list[int] = Field(
        description="Conditions history cannot answer (assumed to hold)."
    )


class DryRunOut(BaseModel):
    runs: list[SimulatedRunOut]
    warnings: list[str]
    truncated: bool


class SceneStateIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    desired: dict[str, Any]


class SceneIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    states: list[SceneStateIn] = Field(min_length=1, max_length=50)


class SceneOut(BaseModel):
    id: UUID
    name: str
    states: list[SceneStateIn]
    version: int
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class ActivationOut(BaseModel):
    outcomes: list[ActionOutcomeOut]
