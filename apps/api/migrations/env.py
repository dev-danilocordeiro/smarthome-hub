"""Alembic environment.

One migration history for the whole monolith; each module owns a Postgres schema
(see ADR 0001). The version table stays in `public` so no module owns it.
"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from smarthome.shared.config import Settings

config = context.config


def database_url() -> str:
    # Tests and tooling may inject a URL explicitly; otherwise fall back to the environment.
    explicit = config.attributes.get("database_url")
    return str(explicit) if explicit else str(Settings().database_url)


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=None,
        include_schemas=True,
        version_table_schema="public",
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await engine.dispose()


def run_migrations_offline() -> None:
    context.configure(url=database_url(), literal_binds=True, version_table_schema="public")
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
