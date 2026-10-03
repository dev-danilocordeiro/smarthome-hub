"""The automation engine, run by the worker (ADR 0010).

Three things make an automation run: a device event from the stream, a hold timer
(`for_s`) coming due, and a schedule slot. All three go through `_start_run`, inside a
transaction that holds the automation's row lock, so cooldown and the loop guard see
every earlier run, and one event (or slot) runs an automation at most once.

Actions are issued after that transaction commits. A crash in between leaves the run
`running`; it is settled as failed later and its actions are not retried: at most once
is the safe side for things like locks.
"""

from contextlib import AbstractContextManager
from datetime import timedelta
from uuid import UUID

import structlog
from opentelemetry import metrics, trace

from smarthome.modules.automations.application.actions import ActionRunner
from smarthome.modules.automations.application.ports import (
    AutomationsUnitOfWork,
    AutomationsUnitOfWorkFactory,
    Causality,
    Devices,
    Directory,
)
from smarthome.modules.automations.domain.definition import DeviceTrigger
from smarthome.modules.automations.domain.engine import (
    Admission,
    Limits,
    admit,
    check_conditions,
    condition_sources,
)
from smarthome.modules.automations.domain.model import Automation, Run, RunStatus
from smarthome.modules.automations.domain.schedule import next_occurrence
from smarthome.shared.audit import AuditEvent
from smarthome.shared.clock import Clock
from smarthome.shared.events import DeviceEvent, DeviceEventKind

log = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

runs_counter = meter.create_counter(
    "smarthome.automations.runs",
    unit="{run}",
    description="Automation runs by status (completed, failed, skipped, suppressed) and cause.",
)
suspended_counter = meter.create_counter(
    "smarthome.automations.suspended",
    unit="{automation}",
    description="Automations suspended by the loop guard.",
)

SYSTEM_ACTOR = "system:automations"
SWEEP_BATCH = 200
# Device changes an automation's own commands can cause; presence they cannot.
CHAINED_EVENTS = frozenset({DeviceEventKind.STATE, DeviceEventKind.TELEMETRY})


def _cause(run: Run) -> str:
    return run.event_key.split(":", 1)[0]  # event | timer | schedule


def actor_for(automation: Automation) -> str:
    return f"automation:{automation.id}"


class AutomationEngine:
    def __init__(
        self,
        uow: AutomationsUnitOfWorkFactory,
        *,
        directory: Directory,
        devices: Devices,
        actions: ActionRunner,
        causality: Causality,
        clock: Clock,
        limits: Limits,
        misfire_grace: timedelta = timedelta(minutes=5),
        interrupted_after: timedelta = timedelta(minutes=5),
    ) -> None:
        self._uow = uow
        self._directory = directory
        self._devices = devices
        self._actions = actions
        self._causality = causality
        self._clock = clock
        self._limits = limits
        self._misfire_grace = misfire_grace
        self._interrupted_after = interrupted_after

    # --- Device events ------------------------------------------------------------------

    async def handle_event(self, event_key: str, event: DeviceEvent) -> int:
        """Apply one event to every armed automation watching its device. Returns the
        number of runs started. Safe to call again with the same event."""
        listening = [
            a
            for a in await self._directory.armed(event.home_id)
            if event.device_id in a.definition.listens_to()
        ]
        if not listening:
            return 0
        depth = await self._causality.depth(event.device_id) if event.kind in CHAINED_EVENTS else 0
        started = 0
        for automation in listening:
            for index, trigger in automation.definition.device_triggers():
                matched = trigger.predicate.observe(event)
                if matched is None:
                    continue
                run = await self._observe(
                    cached=automation,
                    index=index,
                    trigger=trigger,
                    matched=matched,
                    event=event,
                    event_key=f"event:{event_key}",
                    depth=depth,
                )
                if run is not None:
                    started += 1
                    await self._execute(automation, run)
        return started

    async def _observe(
        self,
        *,
        cached: Automation,
        index: int,
        trigger: DeviceTrigger,
        matched: bool,
        event: DeviceEvent,
        event_key: str,
        depth: int,
    ) -> Run | None:
        suspended = False
        async with self._uow() as uow:
            automation = await uow.automations.lock(cached.id)
            if automation is None or not automation.armed or automation.version != cached.version:
                return None  # changed since the directory cached it; the next event sees it
            state = await uow.trigger_states.lock(automation.id, index)
            transition = state.observe(matched, at=event.at, hold=trigger.hold)
            if transition is None:
                return None
            new_state, fire = transition
            await uow.trigger_states.save(automation.id, index, new_state)
            run = None
            if fire:
                run, suspended = await self._start_run(uow, automation, index, event_key, depth)
            await uow.commit()
        if suspended:
            await self._directory.invalidate(automation.home_id)
        return run if run is not None and run.status is RunStatus.RUNNING else None

    # --- Timers and schedules -----------------------------------------------------------

    async def fire_due_timers(self) -> int:
        started = 0
        for automation_id, index in await self._due_timers():
            with self._span("automation timer", automation_id, index):
                run, automation = await self._fire_timer(automation_id, index)
                if run is not None and automation is not None:
                    started += 1
                    await self._execute(automation, run)
        return started

    async def _due_timers(self) -> list[tuple[UUID, int]]:
        async with self._uow() as uow:
            return await uow.trigger_states.due(self._clock.now(), limit=SWEEP_BATCH)

    async def _fire_timer(
        self, automation_id: UUID, index: int
    ) -> tuple[Run | None, Automation | None]:
        now = self._clock.now()
        suspended = False
        async with self._uow() as uow:
            # Same lock order as event handling: automation, then trigger state.
            automation = await uow.automations.lock(automation_id)
            if automation is None or not automation.armed:
                return None, None
            state = await uow.trigger_states.lock(automation_id, index)
            if not state.due(now):
                return None, None  # another worker fired it, or it stopped matching
            fire_at = state.fire_at
            await uow.trigger_states.save(automation_id, index, state.timer_fired())
            key = f"timer:{index}:{fire_at.isoformat() if fire_at else ''}"
            run, suspended = await self._start_run(uow, automation, index, key, depth=0)
            await uow.commit()
        if suspended:
            await self._directory.invalidate(automation.home_id)
        return (run if run is not None and run.status is RunStatus.RUNNING else None), automation

    async def fire_due_schedules(self) -> int:
        async with self._uow() as uow:
            due = await uow.schedules.due(self._clock.now(), limit=SWEEP_BATCH)
        started = 0
        for automation_id, index in due:
            with self._span("automation schedule", automation_id, index):
                run, automation = await self._fire_schedule(automation_id, index)
                if run is not None and automation is not None:
                    started += 1
                    await self._execute(automation, run)
        return started

    async def _fire_schedule(
        self, automation_id: UUID, index: int
    ) -> tuple[Run | None, Automation | None]:
        now = self._clock.now()
        suspended = False
        async with self._uow() as uow:
            automation = await uow.automations.lock(automation_id)
            if automation is None or not automation.armed:
                return None, None
            slot = await uow.schedules.lock(automation_id, index)
            trigger = dict(automation.definition.schedule_triggers()).get(index)
            if slot is None or slot > now or trigger is None:
                return None, None
            await uow.schedules.set_next(
                automation_id, index, next_occurrence(trigger, automation.zone, after=now)
            )
            run = None
            if now - slot > self._misfire_grace:
                # The worker was down at the time: running hours late is worse than not.
                log.warning(
                    "schedule_missed", automation_id=str(automation_id), slot=slot.isoformat()
                )
            else:
                key = f"schedule:{index}:{slot.isoformat()}"
                run, suspended = await self._start_run(uow, automation, index, key, depth=0)
            await uow.commit()
        if suspended:
            await self._directory.invalidate(automation.home_id)
        return (run if run is not None and run.status is RunStatus.RUNNING else None), automation

    @staticmethod
    def _span(name: str, automation_id: UUID, index: int) -> AbstractContextManager[trace.Span]:
        """A root span per timer or slot, so the run and its commands share a trace."""
        return tracer.start_as_current_span(
            name,
            attributes={
                "smarthome.automation.id": str(automation_id),
                "smarthome.automation.trigger": index,
            },
        )

    # --- Running ------------------------------------------------------------------------

    async def _start_run(
        self,
        uow: AutomationsUnitOfWork,
        automation: Automation,
        index: int,
        event_key: str,
        depth: int,
    ) -> tuple[Run | None, bool]:
        """Record the run (or why it did not run) under the automation's lock. Returns
        the run, None if this event already ran it, and whether it was suspended."""
        now = self._clock.now()
        definition = automation.definition
        current = await self._devices.current(automation.home_id, condition_sources(definition))
        check = check_conditions(definition, zone=automation.zone, at=now, current=current)
        suspended = False
        if not check.passed:
            reasons = [f"condition {i} failed" for i in check.failed]
            reasons += [f"condition {i} unknown" for i in check.unknown]
            run = Run.start(
                automation,
                trigger_index=index,
                event_key=event_key,
                depth=depth,
                now=now,
                status=RunStatus.SKIPPED,
                reason=", ".join(reasons),
            )
        else:
            recent = await uow.runs.recent(automation.id, now=now)
            decision = admit(definition, depth=depth, recent=recent, now=now, limits=self._limits)
            status = RunStatus.RUNNING
            if decision.admission is not Admission.RUN:
                status = RunStatus.SUPPRESSED
            run = Run.start(
                automation,
                trigger_index=index,
                event_key=event_key,
                depth=depth,
                now=now,
                status=status,
                reason=decision.reason,
            )
            if decision.admission is Admission.LOOP:
                await self._suspend(uow, automation, decision.reason or "loop suspected")
                suspended = True
        if not await uow.runs.add(run):
            return None, False
        if run.status is not RunStatus.RUNNING:
            runs_counter.add(1, {"status": run.status.value, "cause": _cause(run)})
        return run, suspended

    async def _suspend(
        self, uow: AutomationsUnitOfWork, automation: Automation, reason: str
    ) -> None:
        now = self._clock.now()
        await uow.automations.save(automation.suspend(reason=reason, now=now))
        await uow.trigger_states.replace(automation.id, {})
        await uow.schedules.replace(automation.id, {})
        await uow.audit.append(
            AuditEvent(
                actor=SYSTEM_ACTOR,
                action="automation.suspended",
                target_type="automation",
                target_id=str(automation.id),
                tenant_id=automation.home_id,
                details={"reason": reason, "name": automation.name},
            ),
            occurred_at=now,
        )
        suspended_counter.add(1)
        log.warning("automation_suspended", automation_id=str(automation.id), reason=reason)

    async def _execute(self, automation: Automation, run: Run) -> Run:
        with tracer.start_as_current_span(
            "automation run",
            attributes={
                "smarthome.automation.id": str(automation.id),
                "smarthome.automation.run_id": str(run.id),
                "smarthome.automation.depth": run.depth,
            },
        ):
            steps = await self._actions.plan(automation.home_id, automation.definition.actions)
            # Marked before the commands go out: the device may answer before they return.
            await self._causality.caused(
                {s.device_id for s in steps if s.error is None}, depth=run.depth + 1
            )
            outcomes = await self._actions.run(
                automation.home_id, steps, actor=actor_for(automation)
            )
            finished = run.finish(outcomes, now=self._clock.now())
            async with self._uow() as uow:
                await uow.runs.save(finished)
                await uow.commit()
        runs_counter.add(1, {"status": finished.status.value, "cause": _cause(run)})
        log.info(
            "automation_ran",
            automation_id=str(automation.id),
            run_id=str(run.id),
            status=finished.status.value,
            commands=sum(1 for o in outcomes if o.command_id),
        )
        return finished

    # --- Housekeeping -------------------------------------------------------------------

    async def settle_interrupted(self) -> int:
        """Runs left `running` by a crash between recording and finishing."""
        now = self._clock.now()
        async with self._uow() as uow:
            stale = await uow.runs.lock_running_since(
                now - self._interrupted_after, limit=SWEEP_BATCH
            )
            for run in stale:
                await uow.runs.save(run.interrupt(now=now))
            await uow.commit()
        if stale:
            log.warning("automation_runs_interrupted", count=len(stale))
        return len(stale)

    async def purge_runs(self, *, older_than: timedelta) -> int:
        async with self._uow() as uow:
            purged = await uow.runs.purge(self._clock.now() - older_than)
            await uow.commit()
        return purged
