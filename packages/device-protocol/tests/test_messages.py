import json
import uuid
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from device_protocol import (
    MAX_PAYLOAD_BYTES,
    InvalidMessage,
    MessageKind,
    decode,
    encode,
    schema,
    validate,
)

TS = "2026-10-02T12:00:00Z"
CMD = str(uuid.uuid4())

VALID: dict[MessageKind, dict[str, Any]] = {
    MessageKind.TELEMETRY: {
        "schema_version": "1",
        "message_id": "01J9ZXQ4T8",
        "seq": 42,
        "ts": TS,
        "readings": {"temperature_c": 21.5, "humidity_pct": 48, "battery_pct": 93},
    },
    MessageKind.STATE: {
        "schema_version": "1",
        "message_id": "01J9ZXQ4T9",
        "ts": TS,
        "reported": {"on": True, "brightness_pct": 70},
    },
    MessageKind.COMMAND: {
        "schema_version": "1",
        "command_id": CMD,
        "issued_at": TS,
        "expires_at": "2026-10-02T12:00:30Z",
        "action": "set_state",
        "desired": {"locked": False},
    },
    MessageKind.COMMAND_ACK: {
        "schema_version": "1",
        "command_id": CMD,
        "ts": TS,
        "status": "applied",
    },
    MessageKind.PRESENCE: {"schema_version": "1", "status": "offline", "reason": "connection_lost"},
}


@pytest.mark.parametrize(
    "name", ["common", "telemetry", "state", "command", "command_ack", "presence"]
)
def test_every_shipped_schema_is_itself_a_valid_draft_2020_12_schema(name: str) -> None:
    Draft202012Validator.check_schema(schema(name))


@pytest.mark.parametrize("kind", list(MessageKind))
def test_a_well_formed_message_of_each_kind_validates_and_round_trips(kind: MessageKind) -> None:
    payload = VALID[kind]

    assert decode(kind, encode(kind, payload)) == payload


def mutate(kind: MessageKind, **changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(json.dumps(VALID[kind]))
    for path, value in changes.items():
        target = payload
        *parents, leaf = path.split("__")
        for p in parents:
            target = target[p]
        if value is ...:
            del target[leaf]
        else:
            target[leaf] = value
    return payload


@pytest.mark.parametrize(
    ("kind", "payload", "where"),
    [
        (
            MessageKind.TELEMETRY,
            mutate(MessageKind.TELEMETRY, schema_version="2"),
            "schema_version",
        ),
        (MessageKind.TELEMETRY, mutate(MessageKind.TELEMETRY, readings__co2_ppm=400), "readings"),
        (
            MessageKind.TELEMETRY,
            mutate(MessageKind.TELEMETRY, readings__humidity_pct=140),
            "humidity_pct",
        ),
        (MessageKind.TELEMETRY, mutate(MessageKind.TELEMETRY, readings={}), "readings"),
        (MessageKind.TELEMETRY, mutate(MessageKind.TELEMETRY, seq=-1), "seq"),
        (MessageKind.TELEMETRY, mutate(MessageKind.TELEMETRY, message_id="short"), "message_id"),
        (MessageKind.TELEMETRY, mutate(MessageKind.TELEMETRY, ts=...), "(root)"),
        (MessageKind.TELEMETRY, mutate(MessageKind.TELEMETRY, extra=1), "(root)"),
        (
            MessageKind.STATE,
            mutate(MessageKind.STATE, reported__brightness_pct=101),
            "brightness_pct",
        ),
        (MessageKind.COMMAND, mutate(MessageKind.COMMAND, desired=...), "(root)"),
        (MessageKind.COMMAND, mutate(MessageKind.COMMAND, command_id="not-a-uuid"), "command_id"),
        (MessageKind.COMMAND, mutate(MessageKind.COMMAND, action="self_destruct"), "action"),
        (MessageKind.COMMAND_ACK, mutate(MessageKind.COMMAND_ACK, status="maybe"), "status"),
        (MessageKind.PRESENCE, mutate(MessageKind.PRESENCE, status="sleeping"), "status"),
    ],
)
def test_messages_that_break_the_contract_are_rejected_with_the_offending_field(
    kind: MessageKind, payload: dict[str, Any], where: str
) -> None:
    with pytest.raises(InvalidMessage, match=where.replace("(", r"\(").replace(")", r"\)")):
        validate(kind, payload)


def test_a_command_other_than_set_state_must_not_carry_a_desired_state() -> None:
    payload = mutate(MessageKind.COMMAND, action="reboot")

    with pytest.raises(InvalidMessage):
        validate(MessageKind.COMMAND, payload)
    del payload["desired"]
    validate(MessageKind.COMMAND, payload)


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1, 2]", b"\xff\xfe", b"{" + b" " * MAX_PAYLOAD_BYTES + b"}"],
)
def test_garbage_and_oversized_payloads_never_reach_schema_validation(raw: bytes) -> None:
    with pytest.raises(InvalidMessage):
        decode(MessageKind.TELEMETRY, raw)
