"""Static loop warnings and the dry run over recorded history."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from smarthome.modules.automations.domain.analysis import Node, loop_warnings, loops
from smarthome.modules.automations.domain.definition import Definition, Source, SourceKind
from smarthome.modules.automations.domain.dry_run import Outcome, replayable, simulate
from smarthome.modules.automations.domain.engine import Limits
from smarthome.modules.automations.domain.errors import InvalidRange
from smarthome.modules.automations.domain.model import Scene, SceneState
from tests.unit.automations.fakes import HOME, NOW

ZONE = ZoneInfo("America/Sao_Paulo")
TEMP = Source(SourceKind.TELEMETRY, "climate-1", "temperature_c")


def node(name: str, triggers: list[dict[str, Any]], actions: list[dict[str, Any]]) -> Node:
    return Node(
        uuid4(),
        name,
        Definition.parse({"schema_version": "1", "triggers": triggers, "actions": actions}),
    )


def light_on(device: str = "light-1", **desired: Any) -> dict[str, Any]:
    return {
        "type": "command",
        "device_id": device,
        "action": "set_state",
        "desired": desired or {"on": True},
    }


def when_light(device: str = "light-1", prop: str = "on") -> dict[str, Any]:
    return {
        "type": "device_state",
        "device_id": device,
        "property": prop,
        "op": "eq",
        "value": True,
    }


def test_an_automation_that_changes_what_it_watches_is_a_loop() -> None:
    selfish = node("Selfish", [when_light()], [light_on()])

    assert loop_warnings(selfish.id, [selfish], {}) == [
        "'Selfish' changes a device its own trigger watches and may re-trigger itself"
    ]


def test_two_automations_feeding_each_other_are_reported_together() -> None:
    a = node("A", [when_light("light-1")], [light_on("light-2")])
    b = node("B", [when_light("light-2")], [light_on("light-1")])
    bystander = node("C", [when_light("light-2")], [light_on("plug-1")])

    assert [[n.name for n in c] for c in loops([a, b, bystander], {})] == [["A", "B"]]
    assert loop_warnings(a.id, [a, b, bystander], {}) == [
        "'A', 'B' can trigger each other in a loop"
    ]
    assert loop_warnings(bystander.id, [a, b, bystander], {}) == []


def test_other_properties_of_the_same_device_do_not_feed_a_state_trigger() -> None:
    dims = node("Dims", [when_light(prop="on")], [light_on(brightness_pct=30)])

    assert loops([dims], {}) == []


def test_any_change_to_a_device_feeds_its_telemetry_triggers() -> None:
    heater = node(
        "Heater",
        [
            {
                "type": "telemetry",
                "device_id": "plug-1",
                "metric": "power_w",
                "op": "gt",
                "value": 1000,
            }
        ],
        [light_on("plug-1", on=False)],
    )

    assert len(loops([heater], {})) == 1


def test_scenes_count_as_the_writes_they_contain() -> None:
    scene = Scene.create(
        home_id=HOME,
        name="Evening",
        states=[SceneState("light-1", {"on": True})],
        by="alice",
        now=NOW,
    )
    looping = node("Via scene", [when_light()], [{"type": "scene", "scene_id": str(scene.id)}])

    assert len(loops([looping], {scene.id: scene})) == 1


# --- Dry run ----------------------------------------------------------------------------

START = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
LIMITS = Limits()


def hot(**overrides: Any) -> Definition:
    return Definition.parse(
        {
            "schema_version": "1",
            "triggers": [
                {
                    "type": "telemetry",
                    "device_id": "climate-1",
                    "metric": "temperature_c",
                    "op": "gt",
                    "value": 26,
                }
                | overrides
            ],
            "actions": [light_on("plug-1")],
        }
    )


def series(*values: float, every: timedelta = timedelta(minutes=1)) -> list[tuple[datetime, float]]:
    return [(START + i * every, v) for i, v in enumerate(values)]


def test_a_dry_run_replays_crossings_of_a_threshold() -> None:
    history = {TEMP: series(25, 27, 28, 25, 29)}

    result = simulate(
        hot(),
        zone=ZONE,
        start=START,
        end=START + timedelta(hours=1),
        history=history,
        limits=LIMITS,
    )

    assert [(r.at, r.outcome) for r in result.runs] == [
        (START + timedelta(minutes=1), Outcome.WOULD_RUN),
        (START + timedelta(minutes=4), Outcome.WOULD_RUN),
    ]
    assert replayable(hot()) == [TEMP]


def test_a_dry_run_honours_holds_cooldown_and_conditions() -> None:
    held = hot(for_s=120)
    history = {TEMP: series(25, 27, 27, 27, 25, 27, 25)}

    result = simulate(
        held,
        zone=ZONE,
        start=START,
        end=START + timedelta(hours=1),
        history=history,
        limits=LIMITS,
    )

    # Matching from minute 1; still matching at minute 3 = 120 s later. The second
    # stretch (minute 5 only) is too short.
    assert [(r.at, r.outcome) for r in result.runs] == [
        (START + timedelta(minutes=3), Outcome.WOULD_RUN)
    ]


def test_schedules_replay_with_time_conditions_and_cooldown() -> None:
    parsed = Definition.parse(
        {
            "schema_version": "1",
            "triggers": [
                {"type": "schedule", "at": "07:00"},
                {"type": "schedule", "at": "07:00"},
            ],
            "conditions": [{"type": "time", "weekdays": ["mon", "tue", "wed", "thu", "fri"]}],
            "actions": [light_on()],
            "cooldown_s": 60,
        }
    )

    result = simulate(
        parsed, zone=ZONE, start=START, end=START + timedelta(days=7), history={}, limits=LIMITS
    )

    outcomes = [(r.at.astimezone(ZONE).strftime("%a"), r.outcome) for r in result.runs]
    assert outcomes[:2] == [("Mon", Outcome.WOULD_RUN), ("Mon", Outcome.SUPPRESSED)]
    assert [day for day, o in outcomes if o is Outcome.SKIPPED] == ["Sat", "Sat", "Sun", "Sun"]


def test_what_history_cannot_answer_is_reported() -> None:
    parsed = Definition.parse(
        {
            "schema_version": "1",
            "triggers": [
                {"type": "presence", "device_id": "light-1", "status": "offline"},
                {"type": "schedule", "at": "07:00"},
            ],
            "conditions": [{"type": "presence", "device_id": "light-1", "status": "online"}],
            "actions": [light_on()],
        }
    )

    result = simulate(
        parsed, zone=ZONE, start=START, end=START + timedelta(days=1), history={}, limits=LIMITS
    )

    assert result.warnings == [
        "trigger 0 (presence) cannot be replayed: only telemetry is kept as history"
    ]
    assert [r.unknown_conditions for r in result.runs] == [(0,)]
    assert result.runs[0].outcome is Outcome.WOULD_RUN


@pytest.mark.parametrize("days", [0, 8])
def test_a_dry_run_window_is_between_zero_and_seven_days(days: int) -> None:
    with pytest.raises(InvalidRange):
        simulate(
            hot(),
            zone=ZONE,
            start=START,
            end=START + timedelta(days=days),
            history={},
            limits=LIMITS,
        )
