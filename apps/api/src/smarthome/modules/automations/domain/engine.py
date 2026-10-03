"""How a stream of observations becomes runs: edge detection, holds, conditions and the
guards against runaway automations (ADR 0010). Pure functions; the application feeds
them from the event stream, the schedulers, and (dry run) telemetry history.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from smarthome.modules.automations.domain.definition import (
    Condition,
    Definition,
    Source,
    SourceCondition,
    SourceKind,
)
from smarthome.modules.automations.domain.schedule import time_condition_holds


@dataclass(frozen=True, slots=True)
class TriggerState:
    """What one device trigger last saw.

    `matched` is None until the first observation: a trigger fires on a change from
    "not matching" to "matching", and with no previous observation there is no change
    to speak of (otherwise saving "temperature above 26" on a hot day would fire at
    once). The API primes this from the device's current state when an automation is
    saved, so the first real change is not lost.
    """

    matched: bool | None = None
    observed_at: datetime | None = None
    # Matching, and waiting for its hold (`for_s`) to elapse before firing.
    fire_at: datetime | None = None

    def observe(
        self, matched: bool, *, at: datetime, hold: timedelta
    ) -> "tuple[TriggerState, bool] | None":
        """The next state and whether to fire now; None if the observation is stale.

        Events reach the engine out of order (several consumers, retries); ordering by
        the observation's own time means a late event cannot undo a newer one.
        """
        if self.observed_at is not None and at <= self.observed_at:
            return None
        if not matched:
            return TriggerState(False, at, None), False
        if self.matched is not False:  # already matching, or nothing to compare with
            return replace(self, matched=True, observed_at=at), False
        if hold > timedelta(0):
            return TriggerState(True, at, at + hold), False
        return TriggerState(True, at, None), True

    def due(self, now: datetime) -> bool:
        return self.fire_at is not None and self.fire_at <= now

    def timer_fired(self) -> "TriggerState":
        return replace(self, fire_at=None)


@dataclass(frozen=True, slots=True)
class Observation:
    value: object
    at: datetime


class Verdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"  # no current value (dry run: never recorded)


@dataclass(frozen=True, slots=True)
class ConditionCheck:
    verdicts: tuple[Verdict, ...]

    @property
    def passed(self) -> bool:
        """Unknown counts as failed at run time: when in doubt, do nothing."""
        return all(v is Verdict.PASSED for v in self.verdicts)

    @property
    def failed(self) -> list[int]:
        return [i for i, v in enumerate(self.verdicts) if v is Verdict.FAILED]

    @property
    def unknown(self) -> list[int]:
        return [i for i, v in enumerate(self.verdicts) if v is Verdict.UNKNOWN]


def check_condition(
    condition: Condition,
    *,
    zone: ZoneInfo,
    at: datetime,
    current: Mapping[Source, Observation | None],
) -> Verdict:
    if not isinstance(condition, SourceCondition):
        return Verdict.PASSED if time_condition_holds(condition, zone, at=at) else Verdict.FAILED
    observation = current.get(condition.predicate.source)
    if observation is None:
        return Verdict.UNKNOWN
    if condition.max_age is not None and at - observation.at > condition.max_age:
        return Verdict.FAILED
    return Verdict.PASSED if condition.predicate.test(observation.value) else Verdict.FAILED


def check_conditions(
    definition: Definition,
    *,
    zone: ZoneInfo,
    at: datetime,
    current: Mapping[Source, Observation | None],
) -> ConditionCheck:
    return ConditionCheck(
        tuple(check_condition(c, zone=zone, at=at, current=current) for c in definition.conditions)
    )


def condition_sources(definition: Definition) -> list[Source]:
    return [c.predicate.source for c in definition.conditions if isinstance(c, SourceCondition)]


def prime(
    definition: Definition, current: Mapping[Source, Observation | None]
) -> dict[int, TriggerState]:
    """Initial trigger states from the devices' current values (when saving)."""
    states: dict[int, TriggerState] = {}
    for index, trigger in definition.device_triggers():
        observation = current.get(trigger.predicate.source)
        if observation is not None:
            matched = trigger.predicate.test(observation.value)
            states[index] = TriggerState(matched, observation.at, None)
    return states


# --- Guards ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Limits:
    # Automations that react to devices that other automations changed form a chain;
    # a chain this long is almost certainly a loop.
    max_chain_depth: int = 5
    max_runs_per_minute: int = 20


@dataclass(frozen=True, slots=True)
class RecentRuns:
    last_ran_at: datetime | None
    ran_last_minute: int


class Admission(StrEnum):
    RUN = "run"
    COOLDOWN = "cooldown"  # skipped quietly
    LOOP = "loop"  # suppressed, and the automation is suspended


@dataclass(frozen=True, slots=True)
class Decision:
    admission: Admission
    reason: str | None = None


def admit(
    definition: Definition,
    *,
    depth: int,
    recent: RecentRuns,
    now: datetime,
    limits: Limits,
) -> Decision:
    if depth > limits.max_chain_depth:
        return Decision(
            Admission.LOOP,
            f"loop suspected: triggered by a chain of {depth} automation runs"
            f" (limit {limits.max_chain_depth})",
        )
    if recent.ran_last_minute >= limits.max_runs_per_minute:
        return Decision(
            Admission.LOOP,
            f"loop suspected: {recent.ran_last_minute} runs in the last minute"
            f" (limit {limits.max_runs_per_minute})",
        )
    if recent.last_ran_at is not None and now < recent.last_ran_at + definition.cooldown:
        return Decision(Admission.COOLDOWN, "cooling down")
    return Decision(Admission.RUN)


def chained_kinds() -> frozenset[SourceKind]:
    """Observations an automation's own commands can cause (presence it cannot)."""
    return frozenset({SourceKind.DEVICE_STATE, SourceKind.TELEMETRY})
