import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from smarthome.shared.audit import AuditEvent
from smarthome.shared.clock import SystemClock
from smarthome.shared.infrastructure.audit import PostgresAuditLog, read_chain, verify_tenant_chain

PARALLEL_WRITERS = 20


async def append(engine: AsyncEngine, tenant: object, action: str) -> None:
    async with engine.begin() as conn:
        await PostgresAuditLog(conn).append(
            AuditEvent(
                actor="alice",
                action=action,
                target_type="thing",
                target_id="1",
                tenant_id=tenant,  # type: ignore[arg-type]
                details={"n": action},
            ),
            occurred_at=SystemClock().now(),
        )


async def test_concurrent_writers_extend_one_unbroken_chain(engine: AsyncEngine) -> None:
    tenant = uuid4()

    await asyncio.gather(*(append(engine, tenant, f"a.{i}") for i in range(PARALLEL_WRITERS)))

    async with engine.connect() as conn:
        entries = await read_chain(conn, tenant)
        breaks = await verify_tenant_chain(conn, tenant)
    assert len(entries) == PARALLEL_WRITERS
    assert breaks == []


async def test_chains_of_different_tenants_are_independent(engine: AsyncEngine) -> None:
    first, second = uuid4(), uuid4()
    await asyncio.gather(append(engine, first, "x"), append(engine, second, "y"))

    async with engine.connect() as conn:
        assert [e.prev_hash for e in await read_chain(conn, first)] == [None]
        assert [e.prev_hash for e in await read_chain(conn, second)] == [None]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.entries SET actor = 'mallory' WHERE tenant_id = :t",
        "DELETE FROM audit.entries WHERE tenant_id = :t",
        "TRUNCATE audit.entries",
    ],
)
async def test_the_database_refuses_to_change_or_remove_audit_entries(
    engine: AsyncEngine, statement: str
) -> None:
    tenant = uuid4()
    await append(engine, tenant, "x")

    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(text(statement), {"t": tenant})


async def test_a_rewrite_that_bypasses_the_trigger_is_still_detected(engine: AsyncEngine) -> None:
    tenant = uuid4()
    for action in ("door.unlocked", "guest.invited", "door.locked"):
        await append(engine, tenant, action)

    # A superuser can disable the trigger; the hash chain is what catches that.
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE audit.entries DISABLE TRIGGER entries_append_only"))
        await conn.execute(
            text(
                "UPDATE audit.entries SET actor = 'nobody' "
                "WHERE tenant_id = :t AND action = 'door.unlocked'"
            ),
            {"t": tenant},
        )
        await conn.execute(text("ALTER TABLE audit.entries ENABLE TRIGGER entries_append_only"))

    async with engine.connect() as conn:
        breaks = await verify_tenant_chain(conn, tenant)
    assert len(breaks) == 1
    assert "content" in breaks[0].reason
