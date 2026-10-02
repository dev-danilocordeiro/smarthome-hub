"""Payload validation against the JSON Schemas shipped in `schemas/v1/`.

The schemas are the contract; this module only loads and applies them, so firmware in
any language can validate against the same files.
"""

import json
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from device_protocol.topics import MessageKind

SCHEMA_VERSION = "1"
MAX_PAYLOAD_BYTES = 8 * 1024

SCHEMA_FOR_KIND: dict[MessageKind, str] = {
    MessageKind.TELEMETRY: "telemetry",
    MessageKind.STATE: "state",
    MessageKind.COMMAND: "command",
    MessageKind.COMMAND_ACK: "command_ack",
    MessageKind.PRESENCE: "presence",
}

Payload = dict[str, Any]


class InvalidMessage(ValueError):
    """The payload is not valid JSON, too large, or does not match its schema."""


def schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        resources.files("device_protocol").joinpath(f"schemas/v1/{name}.json").read_text()
    )
    return loaded


@cache
def _validator(kind: MessageKind) -> Draft202012Validator:
    common = schema("common")
    registry: Registry[Any] = Registry().with_resource(
        common["$id"], Resource.from_contents(common)
    )
    main = schema(SCHEMA_FOR_KIND[kind])
    return Draft202012Validator(main, registry=registry, format_checker=FormatChecker())


def validate(kind: MessageKind, payload: Payload) -> Payload:
    errors = sorted(_validator(kind).iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise InvalidMessage(f"{kind.value}: {where}: {first.message}")
    return payload


def decode(kind: MessageKind, raw: bytes) -> Payload:
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise InvalidMessage(
            f"{kind.value}: payload of {len(raw)} bytes exceeds {MAX_PAYLOAD_BYTES}"
        )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidMessage(f"{kind.value}: not JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise InvalidMessage(f"{kind.value}: payload must be a JSON object")
    return validate(kind, payload)


def encode(kind: MessageKind, payload: Payload) -> bytes:
    validate(kind, payload)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
