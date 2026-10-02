from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from smarthome.shared.config import Settings

EXPECTED_MODULE_SCHEMAS = {
    "identity",
    "devices",
    "telemetry",
    "commands",
    "automations",
    "notifications",
    "energy",
}


async def test_migrating_to_head_creates_one_schema_per_module(migrated_database: Settings) -> None:
    engine = create_async_engine(str(migrated_database.database_url))
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT nspname FROM pg_namespace"))
            schemas = {row[0] for row in rows}
    finally:
        await engine.dispose()

    assert schemas >= EXPECTED_MODULE_SCHEMAS


async def test_migrating_to_head_enables_timescaledb(migrated_database: Settings) -> None:
    engine = create_async_engine(str(migrated_database.database_url))
    try:
        async with engine.connect() as conn:
            installed = await conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            )
    finally:
        await engine.dispose()

    assert installed is not None
