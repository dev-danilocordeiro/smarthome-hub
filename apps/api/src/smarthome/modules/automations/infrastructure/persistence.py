"""Postgres adapters for the automations ports (schema `automations`)."""

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from smarthome.modules.automations.application.ports import Revision
from smarthome.modules.automations.domain.definition import Definition
from smarthome.modules.automations.domain.engine import RecentRuns, TriggerState
from smarthome.modules.automations.domain.errors import NameTaken
from smarthome.modules.automations.domain.model import (
    ActionOutcome,
    Automation,
    AutomationStatus,
    Run,
    RunStatus,
    Scene,
    SceneState,
)
from smarthome.shared.infrastructure.audit import PostgresAuditLog

AUTOMATION_COLUMNS = (
    "id, home_id, name, description, enabled, status, status_reason, version, definition,"
    " timezone, created_by, created_at, updated_by, updated_at"
)
RUN_COLUMNS = (
    "id, automation_id, home_id, automation_version, trigger_index, event_key, status, depth,"
    " started_at, finished_at, reason, outcomes, trace_id"
)
SCENE_COLUMNS = "id, home_id, name, states, version, created_by, created_at, updated_by, updated_at"
# Runs that count for cooldown and the loop guard: the ones that acted.
ACTED = "('running', 'completed', 'failed')"


def _name_taken(exc: IntegrityError) -> bool:
    return "name_per_home" in str(exc.orig)


def _automation(row: Row[Any]) -> Automation:
    return Automation(
        id=row.id,
        home_id=row.home_id,
        name=row.name,
        description=row.description,
        enabled=row.enabled,
        status=AutomationStatus(row.status),
        status_reason=row.status_reason,
        version=row.version,
        definition=Definition.parse(row.definition),
        timezone=row.timezone,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def _run(row: Row[Any]) -> Run:
    return Run(
        id=row.id,
        automation_id=row.automation_id,
        home_id=row.home_id,
        automation_version=row.automation_version,
        trigger_index=row.trigger_index,
        event_key=row.event_key,
        status=RunStatus(row.status),
        depth=row.depth,
        started_at=row.started_at,
        finished_at=row.finished_at,
        reason=row.reason,
        outcomes=tuple(
            ActionOutcome(
                device_id=o["device_id"],
                command_id=UUID(o["command_id"]) if o.get("command_id") else None,
                error=o.get("error"),
            )
            for o in row.outcomes
        ),
        trace_id=row.trace_id,
    )


def _scene(row: Row[Any]) -> Scene:
    return Scene(
        id=row.id,
        home_id=row.home_id,
        name=row.name,
        states=tuple(SceneState(s["device_id"], s["desired"]) for s in row.states),
        version=row.version,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


class PostgresAutomations:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, automation: Automation) -> None:
        try:
            async with self._conn.begin_nested():
                await self._conn.execute(
                    text(
                        f"INSERT INTO automations.automations ({AUTOMATION_COLUMNS})"  # noqa: S608
                        " VALUES (:id, :home, :name, :description, :enabled, :status, :reason,"
                        " :version, CAST(:definition AS jsonb), :timezone, :created_by,"
                        " :created_at, :updated_by, :updated_at)"
                    ),
                    self._params(automation),
                )
        except IntegrityError as exc:
            if _name_taken(exc):
                raise NameTaken(automation.name) from exc
            raise

    async def get(self, automation_id: UUID) -> Automation | None:
        row = (
            await self._conn.execute(
                text(f"SELECT {AUTOMATION_COLUMNS} FROM automations.automations WHERE id = :id"),  # noqa: S608
                {"id": automation_id},
            )
        ).first()
        return _automation(row) if row else None

    async def lock(self, automation_id: UUID) -> Automation | None:
        row = (
            await self._conn.execute(
                text(
                    f"SELECT {AUTOMATION_COLUMNS} FROM automations.automations"  # noqa: S608
                    " WHERE id = :id FOR UPDATE"
                ),
                {"id": automation_id},
            )
        ).first()
        return _automation(row) if row else None

    async def for_home(self, home_id: UUID) -> list[Automation]:
        rows = await self._conn.execute(
            text(
                f"SELECT {AUTOMATION_COLUMNS} FROM automations.automations"  # noqa: S608
                " WHERE home_id = :home ORDER BY lower(name)"
            ),
            {"home": home_id},
        )
        return [_automation(r) for r in rows]

    async def save(self, automation: Automation) -> None:
        try:
            async with self._conn.begin_nested():
                await self._conn.execute(
                    text(
                        "UPDATE automations.automations SET name = :name,"
                        " description = :description, enabled = :enabled, status = :status,"
                        " status_reason = :reason, version = :version,"
                        " definition = CAST(:definition AS jsonb), updated_by = :updated_by,"
                        " updated_at = :updated_at WHERE id = :id"
                    ),
                    self._params(automation),
                )
        except IntegrityError as exc:
            if _name_taken(exc):
                raise NameTaken(automation.name) from exc
            raise

    async def delete(self, automation_id: UUID) -> None:
        await self._conn.execute(
            text("DELETE FROM automations.automations WHERE id = :id"), {"id": automation_id}
        )

    async def using_scene(self, home_id: UUID, scene_id: UUID) -> list[Automation]:
        rows = await self._conn.execute(
            text(
                f"SELECT {AUTOMATION_COLUMNS} FROM automations.automations"  # noqa: S608
                " WHERE home_id = :home AND definition->'actions' @> CAST(:probe AS jsonb)"
            ),
            {"home": home_id, "probe": json.dumps([{"type": "scene", "scene_id": str(scene_id)}])},
        )
        return [_automation(r) for r in rows]

    @staticmethod
    def _params(automation: Automation) -> dict[str, Any]:
        return {
            "id": automation.id,
            "home": automation.home_id,
            "name": automation.name,
            "description": automation.description,
            "enabled": automation.enabled,
            "status": automation.status.value,
            "reason": automation.status_reason,
            "version": automation.version,
            "definition": json.dumps(automation.definition.raw),
            "timezone": automation.timezone,
            "created_by": automation.created_by,
            "created_at": automation.created_at,
            "updated_by": automation.updated_by,
            "updated_at": automation.updated_at,
        }


class PostgresRevisions:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, automation: Automation, *, change: str, by: str, at: datetime) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO automations.revisions (automation_id, version, home_id, name,"
                " definition, change, changed_by, changed_at)"
                " VALUES (:id, :version, :home, :name, CAST(:definition AS jsonb), :change,"
                " :by, :at)"
            ),
            {
                "id": automation.id,
                "version": automation.version,
                "home": automation.home_id,
                "name": automation.name,
                "definition": json.dumps(automation.definition.raw),
                "change": change,
                "by": by,
                "at": at,
            },
        )

    async def for_automation(self, automation_id: UUID) -> list[Revision]:
        rows = await self._conn.execute(
            text(
                "SELECT automation_id, version, home_id, name, definition, change, changed_by,"
                " changed_at FROM automations.revisions WHERE automation_id = :id"
                " ORDER BY version DESC"
            ),
            {"id": automation_id},
        )
        return [
            Revision(
                automation_id=r.automation_id,
                version=r.version,
                home_id=r.home_id,
                name=r.name,
                definition=r.definition,
                change=r.change,
                changed_by=r.changed_by,
                changed_at=r.changed_at,
            )
            for r in rows
        ]


class PostgresTriggerStates:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def lock(self, automation_id: UUID, index: int) -> TriggerState:
        # Create-then-lock, so two workers seeing the first event of a trigger serialize.
        await self._conn.execute(
            text(
                "INSERT INTO automations.trigger_states (automation_id, trigger_index)"
                " VALUES (:id, :index) ON CONFLICT DO NOTHING"
            ),
            {"id": automation_id, "index": index},
        )
        row = (
            await self._conn.execute(
                text(
                    "SELECT matched, observed_at, fire_at FROM automations.trigger_states"
                    " WHERE automation_id = :id AND trigger_index = :index FOR UPDATE"
                ),
                {"id": automation_id, "index": index},
            )
        ).one()
        return TriggerState(row.matched, row.observed_at, row.fire_at)

    async def save(self, automation_id: UUID, index: int, state: TriggerState) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO automations.trigger_states"
                " (automation_id, trigger_index, matched, observed_at, fire_at)"
                " VALUES (:id, :index, :matched, :observed_at, :fire_at)"
                " ON CONFLICT (automation_id, trigger_index) DO UPDATE SET"
                " matched = EXCLUDED.matched, observed_at = EXCLUDED.observed_at,"
                " fire_at = EXCLUDED.fire_at"
            ),
            {
                "id": automation_id,
                "index": index,
                "matched": state.matched,
                "observed_at": state.observed_at,
                "fire_at": state.fire_at,
            },
        )

    async def replace(self, automation_id: UUID, states: Mapping[int, TriggerState]) -> None:
        await self._conn.execute(
            text("DELETE FROM automations.trigger_states WHERE automation_id = :id"),
            {"id": automation_id},
        )
        for index, state in states.items():
            await self.save(automation_id, index, state)

    async def due(self, now: datetime, *, limit: int) -> list[tuple[UUID, int]]:
        rows = await self._conn.execute(
            text(
                "SELECT automation_id, trigger_index FROM automations.trigger_states"
                " WHERE fire_at <= :now ORDER BY fire_at LIMIT :limit"
            ),
            {"now": now, "limit": limit},
        )
        return [(r.automation_id, r.trigger_index) for r in rows]


class PostgresSchedules:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def replace(self, automation_id: UUID, next_at: Mapping[int, datetime]) -> None:
        await self._conn.execute(
            text("DELETE FROM automations.schedules WHERE automation_id = :id"),
            {"id": automation_id},
        )
        for index, at in next_at.items():
            await self.set_next(automation_id, index, at)

    async def lock(self, automation_id: UUID, index: int) -> datetime | None:
        row = (
            await self._conn.execute(
                text(
                    "SELECT next_at FROM automations.schedules"
                    " WHERE automation_id = :id AND trigger_index = :index FOR UPDATE"
                ),
                {"id": automation_id, "index": index},
            )
        ).first()
        return row.next_at if row else None

    async def set_next(self, automation_id: UUID, index: int, next_at: datetime) -> None:
        await self._conn.execute(
            text(
                "INSERT INTO automations.schedules (automation_id, trigger_index, next_at)"
                " VALUES (:id, :index, :next_at) ON CONFLICT (automation_id, trigger_index)"
                " DO UPDATE SET next_at = EXCLUDED.next_at"
            ),
            {"id": automation_id, "index": index, "next_at": next_at},
        )

    async def due(self, now: datetime, *, limit: int) -> list[tuple[UUID, int]]:
        rows = await self._conn.execute(
            text(
                "SELECT automation_id, trigger_index FROM automations.schedules"
                " WHERE next_at <= :now ORDER BY next_at LIMIT :limit"
            ),
            {"now": now, "limit": limit},
        )
        return [(r.automation_id, r.trigger_index) for r in rows]


class PostgresRuns:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, run: Run) -> bool:
        span_context = trace.get_current_span().get_span_context()
        trace_id = format(span_context.trace_id, "032x") if span_context.is_valid else None
        inserted = (
            await self._conn.execute(
                text(
                    f"INSERT INTO automations.runs ({RUN_COLUMNS})"  # noqa: S608
                    " VALUES (:id, :automation, :home, :version, :trigger, :key, :status,"
                    " :depth, :started_at, :finished_at, :reason, CAST(:outcomes AS jsonb),"
                    " :trace) ON CONFLICT ON CONSTRAINT one_run_per_event DO NOTHING"
                    " RETURNING id"
                ),
                self._params(run) | {"trace": trace_id},
            )
        ).first()
        return inserted is not None

    async def save(self, run: Run) -> None:
        await self._conn.execute(
            text(
                "UPDATE automations.runs SET status = :status, finished_at = :finished_at,"
                " reason = :reason, outcomes = CAST(:outcomes AS jsonb) WHERE id = :id"
            ),
            self._params(run),
        )

    async def recent(self, automation_id: UUID, *, now: datetime) -> RecentRuns:
        row = (
            await self._conn.execute(
                text(
                    "SELECT max(started_at) AS last_ran_at,"  # noqa: S608 - constant
                    " count(*) FILTER (WHERE started_at > :minute_ago) AS ran_last_minute"
                    f" FROM automations.runs WHERE automation_id = :id AND status IN {ACTED}"
                ),
                {"id": automation_id, "minute_ago": now - timedelta(minutes=1)},
            )
        ).one()
        return RecentRuns(last_ran_at=row.last_ran_at, ran_last_minute=row.ran_last_minute)

    async def for_automation(self, automation_id: UUID, *, limit: int) -> list[Run]:
        rows = await self._conn.execute(
            text(
                f"SELECT {RUN_COLUMNS} FROM automations.runs WHERE automation_id = :id"  # noqa: S608
                " ORDER BY started_at DESC LIMIT :limit"
            ),
            {"id": automation_id, "limit": limit},
        )
        return [_run(r) for r in rows]

    async def lock_running_since(self, before: datetime, *, limit: int) -> list[Run]:
        rows = await self._conn.execute(
            text(
                f"SELECT {RUN_COLUMNS} FROM automations.runs"  # noqa: S608
                " WHERE status = 'running' AND started_at < :before"
                " ORDER BY started_at LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {"before": before, "limit": limit},
        )
        return [_run(r) for r in rows]

    async def purge(self, before: datetime) -> int:
        result = await self._conn.execute(
            text("DELETE FROM automations.runs WHERE started_at < :before AND status <> 'running'"),
            {"before": before},
        )
        return result.rowcount

    @staticmethod
    def _params(run: Run) -> dict[str, Any]:
        return {
            "id": run.id,
            "automation": run.automation_id,
            "home": run.home_id,
            "version": run.automation_version,
            "trigger": run.trigger_index,
            "key": run.event_key,
            "status": run.status.value,
            "depth": run.depth,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "reason": run.reason[:300] if run.reason else None,
            "outcomes": json.dumps(
                [
                    {
                        "device_id": o.device_id,
                        "command_id": str(o.command_id) if o.command_id else None,
                        "error": o.error,
                    }
                    for o in run.outcomes
                ]
            ),
        }


class PostgresScenes:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, scene: Scene) -> None:
        try:
            async with self._conn.begin_nested():
                await self._conn.execute(
                    text(
                        f"INSERT INTO automations.scenes ({SCENE_COLUMNS})"  # noqa: S608
                        " VALUES (:id, :home, :name, CAST(:states AS jsonb), :version,"
                        " :created_by, :created_at, :updated_by, :updated_at)"
                    ),
                    self._params(scene),
                )
        except IntegrityError as exc:
            if _name_taken(exc):
                raise NameTaken(scene.name) from exc
            raise

    async def get(self, scene_id: UUID) -> Scene | None:
        row = (
            await self._conn.execute(
                text(f"SELECT {SCENE_COLUMNS} FROM automations.scenes WHERE id = :id"),  # noqa: S608
                {"id": scene_id},
            )
        ).first()
        return _scene(row) if row else None

    async def lock(self, scene_id: UUID) -> Scene | None:
        row = (
            await self._conn.execute(
                text(
                    f"SELECT {SCENE_COLUMNS} FROM automations.scenes WHERE id = :id FOR UPDATE"  # noqa: S608
                ),
                {"id": scene_id},
            )
        ).first()
        return _scene(row) if row else None

    async def for_home(self, home_id: UUID) -> list[Scene]:
        rows = await self._conn.execute(
            text(
                f"SELECT {SCENE_COLUMNS} FROM automations.scenes WHERE home_id = :home"  # noqa: S608
                " ORDER BY lower(name)"
            ),
            {"home": home_id},
        )
        return [_scene(r) for r in rows]

    async def save(self, scene: Scene) -> None:
        try:
            async with self._conn.begin_nested():
                await self._conn.execute(
                    text(
                        "UPDATE automations.scenes SET name = :name,"
                        " states = CAST(:states AS jsonb), version = :version,"
                        " updated_by = :updated_by, updated_at = :updated_at WHERE id = :id"
                    ),
                    self._params(scene),
                )
        except IntegrityError as exc:
            if _name_taken(exc):
                raise NameTaken(scene.name) from exc
            raise

    async def delete(self, scene_id: UUID) -> None:
        await self._conn.execute(
            text("DELETE FROM automations.scenes WHERE id = :id"), {"id": scene_id}
        )

    @staticmethod
    def _params(scene: Scene) -> dict[str, Any]:
        return {
            "id": scene.id,
            "home": scene.home_id,
            "name": scene.name,
            "states": json.dumps(
                [{"device_id": s.device_id, "desired": s.desired} for s in scene.states]
            ),
            "version": scene.version,
            "created_by": scene.created_by,
            "created_at": scene.created_at,
            "updated_by": scene.updated_by,
            "updated_at": scene.updated_at,
        }


class PostgresAutomationsUnitOfWork:
    automations: PostgresAutomations
    revisions: PostgresRevisions
    trigger_states: PostgresTriggerStates
    schedules: PostgresSchedules
    runs: PostgresRuns
    scenes: PostgresScenes
    audit: PostgresAuditLog

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn: AsyncConnection | None = None
        self._tx: AsyncTransaction | None = None

    async def __aenter__(self) -> Self:
        self._conn = await self._engine.connect()
        self._tx = await self._conn.begin()
        self.automations = PostgresAutomations(self._conn)
        self.revisions = PostgresRevisions(self._conn)
        self.trigger_states = PostgresTriggerStates(self._conn)
        self.schedules = PostgresSchedules(self._conn)
        self.runs = PostgresRuns(self._conn)
        self.scenes = PostgresScenes(self._conn)
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
