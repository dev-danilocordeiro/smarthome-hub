from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from smarthome.shared.audit.model import AuditEntry, entry_hash, verify_chain

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
TENANT = uuid4()


def chain(n: int) -> list[AuditEntry]:
    entries: list[AuditEntry] = []
    prev: bytes | None = None
    for i in range(n):
        fields = {
            "tenant_id": TENANT,
            "occurred_at": T0 + timedelta(seconds=i),
            "actor": "alice",
            "action": f"action.{i}",
            "target_type": "thing",
            "target_id": str(i),
            "details": {"n": i},
        }
        digest = entry_hash(prev_hash=prev, **fields)  # type: ignore[arg-type]
        entries.append(AuditEntry(id=i + 1, trace_id=None, prev_hash=prev, hash=digest, **fields))  # type: ignore[arg-type]
        prev = digest
    return entries


def test_an_untouched_chain_verifies() -> None:
    assert verify_chain(chain(5)) == []


def test_editing_any_field_of_an_entry_is_detected_at_that_entry() -> None:
    entries = chain(5)
    entries[2] = replace(entries[2], actor="mallory")

    breaks = verify_chain(entries)

    assert [b.entry_id for b in breaks] == [3]
    assert "content" in breaks[0].reason


def test_deleting_an_entry_breaks_the_link_of_its_successor() -> None:
    entries = chain(5)
    del entries[1]

    assert [b.entry_id for b in verify_chain(entries)] == [3]


def test_the_hash_does_not_depend_on_the_timezone_the_timestamp_was_written_in() -> None:
    brt = timezone(timedelta(hours=-3))
    args: dict[str, Any] = {"prev_hash": None, "tenant_id": TENANT, "actor": "a", "action": "x"}
    args |= {"target_type": "t", "target_id": "1", "details": {}}

    assert entry_hash(occurred_at=T0, **args) == entry_hash(occurred_at=T0.astimezone(brt), **args)
