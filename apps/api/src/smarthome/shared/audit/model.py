import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Something a person (or a device, or the system) did that must be answerable later."""

    actor: str
    action: str
    target_type: str
    target_id: str
    tenant_id: UUID | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    tenant_id: UUID | None
    occurred_at: datetime
    actor: str
    action: str
    target_type: str
    target_id: str
    details: dict[str, Any]
    trace_id: str | None
    prev_hash: bytes | None
    hash: bytes


@dataclass(frozen=True, slots=True)
class ChainBreak:
    entry_id: int
    reason: str


class AuditLog(Protocol):
    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None: ...


def entry_hash(
    *,
    prev_hash: bytes | None,
    tenant_id: UUID | None,
    occurred_at: datetime,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any],
) -> bytes:
    """sha256(prev_hash || canonical JSON of the entry). Any rewritten field breaks the chain.

    Inputs are normalized the way Postgres stores them (UTC timestamps, JSON-native
    details) so the hash computed before the insert matches the one recomputed on read.
    """
    canonical = json.dumps(
        {
            "tenant_id": str(tenant_id) if tenant_id else None,
            "occurred_at": occurred_at.astimezone(UTC).isoformat(),
            "actor": actor,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "details": json.loads(json.dumps(details, default=str)),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256((prev_hash or b"") + canonical).digest()


def verify_chain(entries: list[AuditEntry]) -> list[ChainBreak]:
    """Check one tenant's entries, in id order, link and hash correctly."""
    breaks: list[ChainBreak] = []
    expected_prev: bytes | None = None
    for entry in entries:
        if entry.prev_hash != expected_prev:
            breaks.append(ChainBreak(entry.id, "prev_hash does not match the previous entry"))
        recomputed = entry_hash(
            prev_hash=entry.prev_hash,
            tenant_id=entry.tenant_id,
            occurred_at=entry.occurred_at,
            actor=entry.actor,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            details=entry.details,
        )
        if recomputed != entry.hash:
            breaks.append(ChainBreak(entry.id, "content does not match its hash"))
        expected_prev = entry.hash
    return breaks
