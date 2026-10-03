"""Dry run: when would this definition have run over a past window?

Replays recorded telemetry and the schedule through the same functions the engine uses
(`TriggerState.observe`, `check_conditions`, `admit`), so the answer matches what the
live engine would have done, as far as history allows:
- device state and presence are not kept as history, so those triggers cannot be
  replayed and those conditions are reported as unknown (and assumed to hold);
- telemetry conditions use the latest recorded reading at that moment.
"""

import heapq
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import count
from zoneinfo import ZoneInfo

from smarthome.modules.automations.domain.definition import Definition, Source, SourceKind
from smarthome.modules.automations.domain.engine import (
    Admission,
    Limits,
    Observation,
    RecentRuns,
    TriggerState,
    Verdict,
    admit,
    check_condition,
)
from smarthome.modules.automations.domain.errors import InvalidRange
from smarthome.modules.automations.domain.schedule import occurrences

MAX_WINDOW = timedelta(days=7)
MAX_RESULTS = 500

# (time, value) pairs per telemetry source, oldest first.
History = Mapping[Source, list[tuple[datetime, float]]]


class Outcome(StrEnum):
    WOULD_RUN = "would_run"
    SKIPPED = "skipped"  # a condition failed
    SUPPRESSED = "suppressed"  # cooldown, or the loop guard would have suspended it


@dataclass(frozen=True, slots=True)
class SimulatedRun:
    at: datetime
    trigger_index: int
    outcome: Outcome
    reason: str | None = None
    failed_conditions: tuple[int, ...] = ()
    unknown_conditions: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DryRun:
    runs: list[SimulatedRun]
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False


def replayable(definition: Definition) -> list[Source]:
    """Telemetry sources whose history the caller must load."""
    return [s for s in definition.sources() if s.kind is SourceKind.TELEMETRY]


def simulate(
    definition: Definition,
    *,
    zone: ZoneInfo,
    start: datetime,
    end: datetime,
    history: History,
    limits: Limits,
) -> DryRun:
    if start >= end:
        raise InvalidRange("`from` must be before `to`")
    if end - start > MAX_WINDOW:
        raise InvalidRange(f"a dry run covers at most {MAX_WINDOW.days} days")
    warnings = [
        f"trigger {i} ({t.predicate.source.kind.value}) cannot be replayed: only telemetry"
        " is kept as history"
        for i, t in definition.device_triggers()
        if t.predicate.source.kind is not SourceKind.TELEMETRY
    ]
    replay = _Replay(definition, zone=zone, end=end, limits=limits)
    replay.load(history, start=start)
    runs, truncated = replay.run()
    return DryRun(runs=runs, warnings=warnings, truncated=truncated)


# Queue entry kinds, in the order they apply at the same instant.
_READING, _TIMER, _SLOT = 0, 1, 2


class _Replay:
    def __init__(self, definition: Definition, *, zone: ZoneInfo, end: datetime, limits: Limits):
        self._definition = definition
        self._zone = zone
        self._end = end
        self._limits = limits
        self._triggers = dict(definition.device_triggers())
        self._states = {index: TriggerState() for index in self._triggers}
        self._latest: dict[Source, Observation] = {}
        self._ran: list[datetime] = []
        self._runs: list[SimulatedRun] = []
        self._sources: list[Source] = []
        # (time, kind, sequence, ref, value); the sequence keeps the heap stable.
        self._queue: list[tuple[datetime, int, int, int, float]] = []
        self._sequence = count()

    def load(self, history: History, *, start: datetime) -> None:
        self._sources = list(history)
        for ref, source in enumerate(self._sources):
            self._queue += [
                (at, _READING, next(self._sequence), ref, value)
                for at, value in history[source]
                if start <= at <= self._end
            ]
        for index, trigger in self._definition.schedule_triggers():
            self._queue += [
                (at, _SLOT, next(self._sequence), index, 0.0)
                for at in occurrences(trigger, self._zone, start=start, end=self._end)
            ]
        heapq.heapify(self._queue)

    def run(self) -> tuple[list[SimulatedRun], bool]:
        while self._queue and len(self._runs) < MAX_RESULTS:
            at, kind, _, ref, value = heapq.heappop(self._queue)
            if kind == _READING:
                self._reading(self._sources[ref], value, at)
            elif kind == _TIMER:
                # Stale if the trigger stopped matching (or re-armed) since.
                if self._states[ref].fire_at == at:
                    self._states[ref] = self._states[ref].timer_fired()
                    self._fire(ref, at)
            else:
                self._fire(ref, at)
        return self._runs, bool(self._queue) and len(self._runs) >= MAX_RESULTS

    def _reading(self, source: Source, value: float, at: datetime) -> None:
        self._latest[source] = Observation(value, at)
        for index, trigger in self._triggers.items():
            if trigger.predicate.source != source:
                continue
            matched = trigger.predicate.test(value)
            result = self._states[index].observe(matched, at=at, hold=trigger.hold)
            if result is None:
                continue
            armed_before = self._states[index].fire_at
            self._states[index], fire_now = result
            fire_at = self._states[index].fire_at
            if fire_now:
                self._fire(index, at)
            elif fire_at is not None and fire_at != armed_before and fire_at <= self._end:
                heapq.heappush(self._queue, (fire_at, _TIMER, next(self._sequence), index, 0.0))

    def _fire(self, index: int, at: datetime) -> None:
        verdicts = [
            check_condition(c, zone=self._zone, at=at, current=self._latest)
            for c in self._definition.conditions
        ]
        failed = tuple(i for i, v in enumerate(verdicts) if v is Verdict.FAILED)
        unknown = tuple(i for i, v in enumerate(verdicts) if v is Verdict.UNKNOWN)
        if failed:
            self._runs.append(SimulatedRun(at, index, Outcome.SKIPPED, None, failed, unknown))
            return
        recent = RecentRuns(
            last_ran_at=self._ran[-1] if self._ran else None,
            ran_last_minute=sum(1 for t in self._ran if t > at - timedelta(minutes=1)),
        )
        decision = admit(self._definition, depth=0, recent=recent, now=at, limits=self._limits)
        if decision.admission is not Admission.RUN:
            self._runs.append(
                SimulatedRun(at, index, Outcome.SUPPRESSED, decision.reason, (), unknown)
            )
            return
        self._ran.append(at)
        self._runs.append(SimulatedRun(at, index, Outcome.WOULD_RUN, None, (), unknown))
