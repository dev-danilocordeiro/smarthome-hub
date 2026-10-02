"""Enable TimescaleDB and create one schema per module.

Revision ID: 0001
Revises:
Create Date: 2026-10-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy on purpose: migrations must not import application code, which keeps
# changing after this revision is merged.
MODULE_SCHEMAS = (
    "identity",
    "devices",
    "telemetry",
    "commands",
    "automations",
    "notifications",
    "energy",
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    for schema in MODULE_SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def downgrade() -> None:
    # RESTRICT: refuse to drop a schema that already holds objects.
    for schema in reversed(MODULE_SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} RESTRICT")
