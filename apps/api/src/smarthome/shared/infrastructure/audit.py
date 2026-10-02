import json
from datetime import datetime
from typing import Any
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from smarthome.shared.audit.model import (
    AuditEntry,
    AuditEvent,
    ChainBreak,
    entry_hash,
    verify_chain,
)

GLOBAL_CHAIN = "audit:global"


class PostgresAuditLog:
    """Appends to `audit.entries` on the caller's connection, so it commits or rolls back
    together with the change being audited.

    Appends to one tenant's chain are serialized with a transaction-scoped advisory lock;
    different tenants never wait on each other.
    """

    def __init__(self, connection: AsyncConnection) -> None:
        self._conn = connection

    async def append(self, event: AuditEvent, *, occurred_at: datetime) -> None:
        chain_key = f"audit:{event.tenant_id}" if event.tenant_id else GLOBAL_CHAIN
        await self._conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": chain_key}
        )
        prev_hash = await self._conn.scalar(
            text(
                "SELECT hash FROM audit.entries WHERE tenant_id IS NOT DISTINCT FROM :tenant "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"tenant": event.tenant_id},
        )
        digest = entry_hash(
            prev_hash=prev_hash,
            tenant_id=event.tenant_id,
            occurred_at=occurred_at,
            actor=event.actor,
            action=event.action,
            target_type=event.target_type,
            target_id=event.target_id,
            details=event.details,
        )
        span_context = trace.get_current_span().get_span_context()
        trace_id = format(span_context.trace_id, "032x") if span_context.is_valid else None
        await self._conn.execute(
            text(
                "INSERT INTO audit.entries (tenant_id, occurred_at, actor, action, target_type,"
                " target_id, details, trace_id, prev_hash, hash)"
                " VALUES (:tenant, :at, :actor, :action, :target_type, :target_id,"
                " CAST(:details AS jsonb), :trace_id, :prev, :hash)"
            ),
            {
                "tenant": event.tenant_id,
                "at": occurred_at,
                "actor": event.actor,
                "action": event.action,
                "target_type": event.target_type,
                "target_id": event.target_id,
                "details": _json(event.details),
                "trace_id": trace_id,
                "prev": prev_hash,
                "hash": digest,
            },
        )


async def read_chain(
    connection: AsyncConnection, tenant_id: UUID | None, *, limit: int | None = None
) -> list[AuditEntry]:
    """Entries of one tenant, oldest first (newest `limit` if given, still oldest first)."""
    query = (
        "SELECT id, tenant_id, occurred_at, actor, action, target_type, target_id, details, "
        "trace_id, prev_hash, hash FROM audit.entries WHERE tenant_id IS NOT DISTINCT FROM :t "
        "ORDER BY id DESC"
    )
    if limit is not None:
        query += " LIMIT :limit"
    rows = (await connection.execute(text(query), {"t": tenant_id, "limit": limit})).all()
    return [
        AuditEntry(
            id=r.id,
            tenant_id=r.tenant_id,
            occurred_at=r.occurred_at,
            actor=r.actor,
            action=r.action,
            target_type=r.target_type,
            target_id=r.target_id,
            details=r.details,
            trace_id=r.trace_id,
            prev_hash=bytes(r.prev_hash) if r.prev_hash is not None else None,
            hash=bytes(r.hash),
        )
        for r in reversed(rows)
    ]


async def verify_tenant_chain(
    connection: AsyncConnection, tenant_id: UUID | None
) -> list[ChainBreak]:
    return verify_chain(await read_chain(connection, tenant_id))


def _json(details: dict[str, Any]) -> str:
    return json.dumps(details, default=str, sort_keys=True)
