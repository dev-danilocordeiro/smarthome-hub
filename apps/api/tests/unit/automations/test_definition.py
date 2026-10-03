"""The automation DSL: what it accepts, and how it says what is wrong."""

from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from device_protocol import DeviceKind
from smarthome.modules.automations.domain.definition import (
    CommandStep,
    Definition,
    DeviceTrigger,
    Op,
    SceneStep,
    ScheduleTrigger,
    Source,
    SourceKind,
    TimeCondition,
    definition_schema,
)
from smarthome.modules.automations.domain.errors import InvalidDefinition
from smarthome.shared.events import DeviceEvent, DeviceEventKind
from tests.unit.automations.fakes import HOME

KINDS = {
    "light-1": DeviceKind.LIGHT,
    "motion-1": DeviceKind.MOTION_SENSOR,
    "climate-1": DeviceKind.CLIMATE_SENSOR,
    "thermo-1": DeviceKind.THERMOSTAT,
}
SCENE = "7a1d2c5e-0000-4000-8000-00000000a11c"


def definition(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "triggers": [
            {
                "type": "telemetry",
                "device_id": "motion-1",
                "metric": "motion",
                "op": "eq",
                "value": True,
            },
        ],
        "actions": [
            {
                "type": "command",
                "device_id": "light-1",
                "action": "set_state",
                "desired": {"on": True},
            },
        ],
    } | overrides


def test_a_complete_definition_parses_into_triggers_conditions_and_actions() -> None:
    parsed = Definition.parse(
        definition(
            triggers=[
                {
                    "type": "telemetry",
                    "device_id": "motion-1",
                    "metric": "motion",
                    "op": "eq",
                    "value": True,
                    "for_s": 5,
                },
                {
                    "type": "device_state",
                    "device_id": "light-1",
                    "property": "brightness_pct",
                    "op": "gte",
                    "value": 50,
                },
                {"type": "presence", "device_id": "light-1", "status": "offline"},
                {"type": "schedule", "at": "22:30", "weekdays": ["sat", "sun"]},
            ],
            conditions=[
                {"type": "time", "after": "18:00", "before": "06:00"},
                {
                    "type": "telemetry",
                    "device_id": "climate-1",
                    "metric": "illuminance_lux",
                    "op": "lt",
                    "value": 40,
                },
            ],
            actions=[
                {"type": "command", "device_id": "light-1", "action": "identify"},
                {"type": "scene", "scene_id": SCENE},
            ],
            cooldown_s=60,
        )
    )

    motion, brightness, presence, schedule = parsed.triggers
    assert isinstance(motion, DeviceTrigger)
    assert motion.hold == timedelta(seconds=5)
    assert motion.predicate.source == Source(SourceKind.TELEMETRY, "motion-1", "motion")
    assert isinstance(brightness, DeviceTrigger)
    assert brightness.predicate.op is Op.GTE
    assert isinstance(presence, DeviceTrigger)
    assert presence.predicate.value is False  # offline = "online is false"
    assert schedule == ScheduleTrigger(time(22, 30), frozenset({5, 6}))
    assert parsed.conditions[0] == TimeCondition(time(18), time(6), frozenset(range(7)))
    assert parsed.actions == (CommandStep("light-1", "identify"), SceneStep(UUID(SCENE)))
    assert parsed.cooldown == timedelta(minutes=1)
    assert parsed.listens_to() == {"motion-1", "light-1"}
    assert parsed.scene_ids() == {UUID(SCENE)}


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        (definition(schema_version="2"), "schema_version"),
        (definition(triggers=[]), "triggers"),
        (definition(triggers=[{"type": "sunrise"}]), "triggers/0"),
        (
            definition(actions=[{"type": "command", "device_id": "light-1", "action": "reboot"}]),
            "actions/0/action",
        ),
        (definition(cooldown_s=-1), "cooldown_s"),
        (definition(extra=True), "(root)"),
    ],
)
def test_schema_violations_point_at_the_offending_field(raw: dict[str, Any], path: str) -> None:
    with pytest.raises(InvalidDefinition) as caught:
        Definition.parse(raw)

    assert caught.value.path.startswith(path)


@pytest.mark.parametrize(
    "raw",
    [
        definition(
            triggers=[
                {
                    "type": "device_state",
                    "device_id": "light-1",
                    "property": "on",
                    "op": "gt",
                    "value": True,
                }
            ]
        ),
        definition(
            triggers=[
                {
                    "type": "device_state",
                    "device_id": "thermo-1",
                    "property": "hvac_mode",
                    "op": "lt",
                    "value": "heat",
                }
            ]
        ),
        definition(actions=[{"type": "command", "device_id": "light-1", "action": "set_state"}]),
        definition(
            actions=[
                {
                    "type": "command",
                    "device_id": "light-1",
                    "action": "identify",
                    "desired": {"on": True},
                }
            ]
        ),
        definition(conditions=[{"type": "time", "after": "10:00", "before": "10:00"}]),
    ],
)
def test_rules_the_schema_cannot_express_are_enforced(raw: dict[str, Any]) -> None:
    with pytest.raises(InvalidDefinition):
        Definition.parse(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            definition(actions=[{"type": "command", "device_id": "light-9", "action": "identify"}]),
            "no device 'light-9'",
        ),
        (
            definition(
                triggers=[
                    {
                        "type": "device_state",
                        "device_id": "motion-1",
                        "property": "on",
                        "op": "eq",
                        "value": True,
                    }
                ]
            ),
            "motion_sensor has no property 'on'",
        ),
        (
            definition(
                triggers=[
                    {
                        "type": "device_state",
                        "device_id": "light-1",
                        "property": "brightness_pct",
                        "op": "eq",
                        "value": 150,
                    }
                ]
            ),
            "not a possible brightness_pct",
        ),
        (
            definition(
                triggers=[
                    {
                        "type": "telemetry",
                        "device_id": "climate-1",
                        "metric": "co2_ppm",
                        "op": "gt",
                        "value": 1000,
                    }
                ]
            ),
            "unknown metric",
        ),
        (
            definition(
                triggers=[
                    {
                        "type": "telemetry",
                        "device_id": "motion-1",
                        "metric": "motion",
                        "op": "eq",
                        "value": 1,
                    }
                ]
            ),
            "motion is a boolean",
        ),
        (
            definition(
                actions=[
                    {
                        "type": "command",
                        "device_id": "light-1",
                        "action": "set_state",
                        "desired": {"locked": True},
                    }
                ]
            ),
            "light has no properties ['locked']",
        ),
        (
            definition(
                actions=[
                    {
                        "type": "command",
                        "device_id": "thermo-1",
                        "action": "set_state",
                        "desired": {"target_temp_c": 80},
                    }
                ]
            ),
            "target_temp_c",
        ),
    ],
)
def test_references_are_checked_against_the_homes_devices(
    raw: dict[str, Any], message: str
) -> None:
    parsed = Definition.parse(raw)

    with pytest.raises(InvalidDefinition, match=message.replace("[", r"\[").replace("]", r"\]")):
        parsed.check_devices(KINDS)


def test_the_schema_is_served_as_a_json_schema_document() -> None:
    schema = definition_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"] == {"const": "1"}


def test_a_trigger_reads_only_events_of_its_own_device_and_kind() -> None:
    trigger = Definition.parse(definition()).triggers[0]
    assert isinstance(trigger, DeviceTrigger)
    at = datetime(2026, 10, 5, tzinfo=UTC)

    def event(device: str, kind: DeviceEventKind, data: dict[str, Any]) -> DeviceEvent:
        return DeviceEvent(HOME, device, kind, at, data)

    observe = trigger.predicate.observe
    assert observe(event("motion-1", DeviceEventKind.TELEMETRY, {"motion": True})) is True
    assert observe(event("motion-1", DeviceEventKind.TELEMETRY, {"motion": False})) is False
    assert observe(event("motion-1", DeviceEventKind.TELEMETRY, {"battery_pct": 80})) is None
    assert observe(event("motion-2", DeviceEventKind.TELEMETRY, {"motion": True})) is None
    assert observe(event("motion-1", DeviceEventKind.STATE, {"motion": True})) is None


numbers = st.floats(allow_nan=False, allow_infinity=False) | st.integers()


@given(observed=numbers, expected=numbers)
def test_ordering_operators_partition_the_numbers(observed: float, expected: float) -> None:
    assert Op.GT.test(observed, expected) != Op.LTE.test(observed, expected)
    assert Op.LT.test(observed, expected) != Op.GTE.test(observed, expected)
    assert Op.EQ.test(observed, expected) != Op.NE.test(observed, expected)


@pytest.mark.parametrize(
    ("op", "observed", "expected", "result"),
    [
        (Op.EQ, True, True, True),
        (Op.EQ, 1.0, True, True),  # booleans are stored as 1.0 / 0.0 in history
        (Op.NE, 0.0, True, True),
        (Op.EQ, True, 1, False),  # a boolean is not the number 1
        (Op.GT, "warm", 20, False),
        (Op.EQ, "heat", "heat", True),
        (Op.GT, "b", "a", False),
    ],
)
def test_values_of_another_type_never_match(
    op: Op, observed: object, expected: object, result: bool
) -> None:
    assert op.test(observed, expected) is result
