"""Edge detection, holds, conditions and the loop guard (pure domain)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from smarthome.modules.automations.domain.definition import Definition, Source, SourceKind
from smarthome.modules.automations.domain.engine import (
    Admission,
    Limits,
    Observation,
    RecentRuns,
    TriggerState,
    Verdict,
    admit,
    check_conditions,
    prime,
)

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
NO_HOLD = timedelta(0)
ZONE = ZoneInfo("America/Sao_Paulo")
LUX = Source(SourceKind.TELEMETRY, "climate-1", "illuminance_lux")


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def step(
    state: TriggerState, matched: bool, seconds: float, hold: float = 0
) -> tuple[TriggerState, bool]:
    result = state.observe(matched, at=at(seconds), hold=timedelta(seconds=hold))
    assert result is not None
    return result


def test_the_first_observation_only_learns_the_current_state() -> None:
    state, fired = step(TriggerState(), True, 0)

    assert not fired
    assert state.matched is True


def test_fires_on_the_change_to_matching_and_not_while_it_keeps_matching() -> None:
    state, _ = step(TriggerState(), False, 0)

    state, fired = step(state, True, 1)
    assert fired
    state, fired = step(state, True, 2)
    assert not fired
    state, fired = step(state, False, 3)
    assert not fired
    _, fired = step(state, True, 4)
    assert fired


def test_late_and_duplicate_observations_are_ignored() -> None:
    state, _ = step(TriggerState(), False, 10)

    assert state.observe(True, at=at(5), hold=NO_HOLD) is None
    assert state.observe(True, at=at(10), hold=NO_HOLD) is None


def test_a_hold_fires_only_if_it_keeps_matching_for_long_enough() -> None:
    state, _ = step(TriggerState(), False, 0)

    state, fired = step(state, True, 1, hold=60)
    assert not fired
    assert state.fire_at == at(61)
    assert not state.due(at(60))
    assert state.due(at(61))

    state, _ = step(state, True, 30, hold=60)  # still matching: the timer stands
    assert state.fire_at == at(61)

    state, _ = step(state, False, 40, hold=60)  # stopped matching: cancelled
    assert state.fire_at is None
    assert not state.due(at(100))


def test_a_fired_timer_does_not_fire_again_until_the_next_change() -> None:
    state, _ = step(TriggerState(), False, 0)
    state, _ = step(state, True, 1, hold=5)

    state = state.timer_fired()
    state, fired = step(state, True, 10, hold=5)

    assert not fired
    assert state.fire_at is None


def definition(**overrides: Any) -> Definition:
    return Definition.parse(
        {
            "schema_version": "1",
            "triggers": [{"type": "schedule", "at": "19:00"}],
            "actions": [{"type": "command", "device_id": "light-1", "action": "identify"}],
        }
        | overrides
    )


def test_conditions_must_all_hold_and_unknown_counts_as_not_holding() -> None:
    parsed = definition(
        conditions=[
            {"type": "time", "after": "18:00"},
            {
                "type": "telemetry",
                "device_id": "climate-1",
                "metric": "illuminance_lux",
                "op": "lt",
                "value": 40,
                "max_age_s": 600,
            },
        ]
    )
    evening = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)  # 19:00 local

    dark = check_conditions(parsed, zone=ZONE, at=evening, current={LUX: Observation(10, evening)})
    bright = check_conditions(
        parsed, zone=ZONE, at=evening, current={LUX: Observation(300, evening)}
    )
    unknown = check_conditions(parsed, zone=ZONE, at=evening, current={})
    stale = check_conditions(
        parsed,
        zone=ZONE,
        at=evening,
        current={LUX: Observation(10, evening - timedelta(minutes=11))},
    )

    assert dark.passed
    assert not bright.passed
    assert bright.failed == [1]
    assert not unknown.passed
    assert unknown.unknown == [1]
    assert stale.verdicts == (Verdict.PASSED, Verdict.FAILED)


def test_priming_records_what_each_device_trigger_sees_now() -> None:
    parsed = definition(
        triggers=[
            {
                "type": "telemetry",
                "device_id": "climate-1",
                "metric": "illuminance_lux",
                "op": "lt",
                "value": 40,
            },
            {"type": "presence", "device_id": "light-1", "status": "offline"},
            {"type": "schedule", "at": "19:00"},
        ]
    )

    states = prime(parsed, {LUX: Observation(10, T0)})

    assert states == {0: TriggerState(True, T0, None)}


LIMITS = Limits(max_chain_depth=3, max_runs_per_minute=5)


@pytest.mark.parametrize(
    ("depth", "recent", "admission"),
    [
        (0, RecentRuns(None, 0), Admission.RUN),
        (3, RecentRuns(None, 0), Admission.RUN),
        (4, RecentRuns(None, 0), Admission.LOOP),
        (0, RecentRuns(T0 - timedelta(seconds=30), 4), Admission.COOLDOWN),
        (0, RecentRuns(T0 - timedelta(seconds=90), 4), Admission.RUN),
        (0, RecentRuns(T0 - timedelta(seconds=90), 5), Admission.LOOP),
    ],
)
def test_the_guard_runs_cools_down_or_calls_it_a_loop(
    depth: int, recent: RecentRuns, admission: Admission
) -> None:
    decision = admit(definition(cooldown_s=60), depth=depth, recent=recent, now=T0, limits=LIMITS)

    assert decision.admission is admission
    if admission is Admission.LOOP:
        assert decision.reason is not None
        assert decision.reason.startswith("loop suspected")
