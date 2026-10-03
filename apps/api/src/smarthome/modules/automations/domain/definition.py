"""The automation DSL (v1): parsing, validation and the questions the engine asks of it.

The JSON Schema in `schemas/automation-v1.json` is the contract (the web editor uses it
too); this module applies it and adds the rules a schema cannot express.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker

from device_protocol import STATE_PROPERTIES, DeviceKind, InvalidMessage, MessageKind, validate
from device_protocol import schema as protocol_schema
from smarthome.modules.automations.domain.errors import InvalidDefinition
from smarthome.shared.events import DeviceEvent, DeviceEventKind

SCHEMA_VERSION = "1"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # index = date.weekday()
ALL_WEEK = frozenset(range(7))
TELEMETRY_METRICS: Mapping[str, dict[str, Any]] = protocol_schema("telemetry")["properties"][
    "readings"
]["properties"]


def definition_schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        resources.files("smarthome.modules.automations.domain")
        .joinpath("schemas/automation-v1.json")
        .read_text()
    )
    return loaded


@cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(definition_schema(), format_checker=FormatChecker())


class Op(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"

    @property
    def ordering(self) -> bool:
        return self not in (Op.EQ, Op.NE)

    def test(self, observed: object, expected: object) -> bool:
        """False when the observation cannot be compared (missing or another type).

        Booleans also accept 1.0 / 0.0: telemetry history stores them as numbers.
        """
        if isinstance(expected, bool):
            if isinstance(observed, bool):
                actual = observed
            elif isinstance(observed, int | float):
                actual = observed >= 0.5  # noqa: PLR2004 - 1.0 / 0.0
            else:
                return False
            return {Op.EQ: actual == expected, Op.NE: actual != expected}.get(self, False)
        if isinstance(expected, int | float):
            if isinstance(observed, bool) or not isinstance(observed, int | float):
                return False
            return {
                Op.EQ: observed == expected,
                Op.NE: observed != expected,
                Op.GT: observed > expected,
                Op.GTE: observed >= expected,
                Op.LT: observed < expected,
                Op.LTE: observed <= expected,
            }[self]
        if not isinstance(observed, str):
            return False
        return {Op.EQ: observed == expected, Op.NE: observed != expected}.get(self, False)


class SourceKind(StrEnum):
    DEVICE_STATE = "device_state"
    TELEMETRY = "telemetry"
    PRESENCE = "presence"


EVENT_FOR_SOURCE = {
    SourceKind.DEVICE_STATE: DeviceEventKind.STATE,
    SourceKind.TELEMETRY: DeviceEventKind.TELEMETRY,
    SourceKind.PRESENCE: DeviceEventKind.PRESENCE,
}
ONLINE = "online"


@dataclass(frozen=True, slots=True)
class Source:
    """One observable value of one device: a twin property, a metric, or presence."""

    kind: SourceKind
    device_id: str
    key: str  # property, metric, or "online"

    def read(self, event: DeviceEvent) -> object | None:
        """The value this event reports for the source, or None if it says nothing
        about it (another device, kind, or a telemetry message without the metric)."""
        if event.device_id != self.device_id or event.kind is not EVENT_FOR_SOURCE[self.kind]:
            return None
        return event.data.get(self.key)


@dataclass(frozen=True, slots=True)
class Predicate:
    source: Source
    op: Op
    value: object

    def test(self, observed: object) -> bool:
        return self.op.test(observed, self.value)

    def observe(self, event: DeviceEvent) -> bool | None:
        observed = self.source.read(event)
        return None if observed is None else self.test(observed)


@dataclass(frozen=True, slots=True)
class DeviceTrigger:
    predicate: Predicate
    hold: timedelta = timedelta(0)


@dataclass(frozen=True, slots=True)
class ScheduleTrigger:
    at: time
    weekdays: frozenset[int] = ALL_WEEK


Trigger = DeviceTrigger | ScheduleTrigger


@dataclass(frozen=True, slots=True)
class SourceCondition:
    predicate: Predicate
    # Telemetry only: a reading older than this does not count.
    max_age: timedelta | None = None


@dataclass(frozen=True, slots=True)
class TimeCondition:
    after: time | None = None
    before: time | None = None
    weekdays: frozenset[int] = ALL_WEEK


Condition = SourceCondition | TimeCondition


@dataclass(frozen=True, slots=True)
class CommandStep:
    device_id: str
    action: str  # set_state | identify
    desired: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SceneStep:
    scene_id: UUID


Action = CommandStep | SceneStep


@dataclass(frozen=True, slots=True)
class Definition:
    triggers: tuple[Trigger, ...]
    conditions: tuple[Condition, ...]
    actions: tuple[Action, ...]
    cooldown: timedelta
    raw: dict[str, Any]

    @staticmethod
    def parse(raw: object) -> "Definition":
        if not isinstance(raw, dict):
            raise InvalidDefinition("definition must be a JSON object")
        errors = sorted(_validator().iter_errors(raw), key=lambda e: list(e.absolute_path))
        if errors:
            # oneOf failures are reported against the item; the most specific
            # sub-error says what is actually wrong.
            first = errors[0]
            best = min(first.context or [first], key=lambda e: -len(e.absolute_path))
            where = "/".join(str(p) for p in best.absolute_path) or "(root)"
            raise InvalidDefinition(best.message, path=where)
        return Definition(
            triggers=tuple(_trigger(t, f"triggers/{i}") for i, t in enumerate(raw["triggers"])),
            conditions=tuple(
                _condition(c, f"conditions/{i}") for i, c in enumerate(raw.get("conditions", []))
            ),
            actions=tuple(_action(a, f"actions/{i}") for i, a in enumerate(raw["actions"])),
            cooldown=timedelta(seconds=raw.get("cooldown_s", 0)),
            raw=json.loads(json.dumps(raw)),  # a private copy
        )

    # --- What the engine and the API ask --------------------------------------------

    def device_triggers(self) -> list[tuple[int, DeviceTrigger]]:
        return [(i, t) for i, t in enumerate(self.triggers) if isinstance(t, DeviceTrigger)]

    def schedule_triggers(self) -> list[tuple[int, ScheduleTrigger]]:
        return [(i, t) for i, t in enumerate(self.triggers) if isinstance(t, ScheduleTrigger)]

    def listens_to(self) -> frozenset[str]:
        return frozenset(t.predicate.source.device_id for _, t in self.device_triggers())

    def sources(self) -> list[Source]:
        """Everything the definition reads, triggers and conditions alike."""
        found = [t.predicate.source for _, t in self.device_triggers()]
        found += [c.predicate.source for c in self.conditions if isinstance(c, SourceCondition)]
        return list(dict.fromkeys(found))

    def scene_ids(self) -> frozenset[UUID]:
        return frozenset(a.scene_id for a in self.actions if isinstance(a, SceneStep))

    def device_ids(self) -> frozenset[str]:
        """Devices referenced directly (scenes are resolved by the caller)."""
        ids = {s.device_id for s in self.sources()}
        ids |= {a.device_id for a in self.actions if isinstance(a, CommandStep)}
        return frozenset(ids)

    def check_devices(self, kinds: Mapping[str, DeviceKind]) -> None:
        """Raise unless every referenced device exists (`kinds` holds the home's devices)
        and every property, metric and desired state fits its kind."""
        for i, trigger in self.device_triggers():
            _check_predicate(trigger.predicate, kinds, f"triggers/{i}")
        for i, condition in enumerate(self.conditions):
            if isinstance(condition, SourceCondition):
                _check_predicate(condition.predicate, kinds, f"conditions/{i}")
        for i, action in enumerate(self.actions):
            if isinstance(action, CommandStep):
                kind = _kind(kinds, action.device_id, f"actions/{i}")
                if action.desired is not None:
                    check_desired(kind, action.desired, path=f"actions/{i}/desired")


def check_desired(kind: DeviceKind, desired: dict[str, Any], *, path: str) -> None:
    unknown = set(desired) - STATE_PROPERTIES[kind]
    if unknown:
        raise InvalidDefinition(f"{kind.value} has no properties {sorted(unknown)}", path=path)
    message = {
        "schema_version": "1",
        "command_id": str(uuid4()),
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-01T00:00:30Z",
        "action": "set_state",
        "desired": desired,
    }
    try:
        validate(MessageKind.COMMAND, message)
    except InvalidMessage as exc:
        raise InvalidDefinition(str(exc).split(": ", 1)[-1], path=path) from exc


# --- Parsing helpers --------------------------------------------------------------------


def _clock(raw: str) -> time:
    hours, minutes = raw.split(":")
    return time(int(hours), int(minutes))


def _weekdays(raw: list[str] | None) -> frozenset[int]:
    return ALL_WEEK if raw is None else frozenset(WEEKDAYS.index(d) for d in raw)


def _predicate(raw: dict[str, Any], path: str) -> Predicate:
    kind = SourceKind(raw["type"])
    if kind is SourceKind.PRESENCE:
        return Predicate(Source(kind, raw["device_id"], ONLINE), Op.EQ, raw["status"] == ONLINE)
    key = raw["property"] if kind is SourceKind.DEVICE_STATE else raw["metric"]
    op, value = Op(raw["op"]), raw["value"]
    if op.ordering and (isinstance(value, bool) or not isinstance(value, int | float)):
        raise InvalidDefinition(f"`{op.value}` compares numbers only", path=f"{path}/value")
    return Predicate(Source(kind, raw["device_id"], key), op, value)


def _trigger(raw: dict[str, Any], path: str) -> Trigger:
    if raw["type"] == "schedule":
        return ScheduleTrigger(_clock(raw["at"]), _weekdays(raw.get("weekdays")))
    return DeviceTrigger(_predicate(raw, path), timedelta(seconds=raw.get("for_s", 0)))


def _condition(raw: dict[str, Any], path: str) -> Condition:
    if raw["type"] == "time":
        after = _clock(raw["after"]) if "after" in raw else None
        before = _clock(raw["before"]) if "before" in raw else None
        if after is not None and after == before:
            raise InvalidDefinition("`after` and `before` must differ", path=path)
        return TimeCondition(after, before, _weekdays(raw.get("weekdays")))
    max_age = timedelta(seconds=raw.get("max_age_s", 900)) if raw["type"] == "telemetry" else None
    return SourceCondition(_predicate(raw, path), max_age)


def _action(raw: dict[str, Any], path: str) -> Action:
    if raw["type"] == "scene":
        return SceneStep(UUID(raw["scene_id"]))
    desired = raw.get("desired")
    if raw["action"] == "set_state" and desired is None:
        raise InvalidDefinition("set_state needs `desired`", path=path)
    if raw["action"] != "set_state" and desired is not None:
        raise InvalidDefinition(f"{raw['action']} takes no `desired`", path=path)
    return CommandStep(raw["device_id"], raw["action"], desired)


def _kind(kinds: Mapping[str, DeviceKind], device_id: str, path: str) -> DeviceKind:
    kind = kinds.get(device_id)
    if kind is None:
        raise InvalidDefinition(f"no device {device_id!r} in this home", path=f"{path}/device_id")
    return kind


def _check_predicate(predicate: Predicate, kinds: Mapping[str, DeviceKind], path: str) -> None:
    source = predicate.source
    kind = _kind(kinds, source.device_id, path)
    if source.kind is SourceKind.PRESENCE:
        return
    if source.kind is SourceKind.DEVICE_STATE:
        if source.key not in STATE_PROPERTIES[kind]:
            raise InvalidDefinition(
                f"{kind.value} has no property {source.key!r}", path=f"{path}/property"
            )
        try:
            validate(
                MessageKind.STATE,
                {
                    "schema_version": "1",
                    "message_id": "validation",
                    "ts": "2026-01-01T00:00:00Z",
                    "reported": {source.key: predicate.value},
                },
            )
        except InvalidMessage as exc:
            raise InvalidDefinition(
                f"{predicate.value!r} is not a possible {source.key}", path=f"{path}/value"
            ) from exc
        return
    spec = TELEMETRY_METRICS.get(source.key)
    if spec is None:
        raise InvalidDefinition(f"unknown metric {source.key!r}", path=f"{path}/metric")
    if (spec["type"] == "boolean") != isinstance(predicate.value, bool):
        raise InvalidDefinition(
            f"{source.key} is a {spec['type']}; compare it with one", path=f"{path}/value"
        )
    if spec["type"] == "boolean" and predicate.op.ordering:
        raise InvalidDefinition("booleans support eq and ne only", path=f"{path}/op")
