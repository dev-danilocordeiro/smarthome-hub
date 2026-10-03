"""Hourly energy consumption per device, the rollup cursor, and tariffs.

See ADR 0012.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Derived from telemetry counters, but kept on its own: raw readings expire after
    # 30 days and a year of hourly rows per device is ~9k rows.
    op.execute(
        """
        CREATE TABLE energy.hourly (
            device_id  text NOT NULL,
            hour       timestamptz NOT NULL CHECK (
                hour = date_trunc('hour', hour AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            ),
            home_id    uuid NOT NULL,
            wh         double precision NOT NULL CHECK (wh >= 0),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (device_id, hour)
        )
        """
    )
    op.execute("CREATE INDEX hourly_by_home ON energy.hourly (home_id, hour)")

    # One row: how far the rollup has got. Locked with an advisory lock, not FOR UPDATE,
    # so a second worker skips the run instead of queueing behind the first.
    op.execute(
        """
        CREATE TABLE energy.rollup_cursor (
            id              boolean PRIMARY KEY DEFAULT true CHECK (id),
            computed_until  timestamptz NOT NULL
        )
        """
    )

    # Prices are numeric, never float: they are money. Periods are a small JSON array
    # validated by the domain; the base price and currency are columns so the schema
    # itself refuses nonsense.
    op.execute(
        """
        CREATE TABLE energy.tariffs (
            home_id             uuid PRIMARY KEY,
            currency            text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
            base_price          numeric(12, 6) NOT NULL CHECK (base_price >= 0),
            periods             jsonb NOT NULL CHECK (jsonb_typeof(periods) = 'array'),
            monthly_budget_kwh  numeric(12, 3) CHECK (monthly_budget_kwh > 0),
            timezone            text NOT NULL,
            version             integer NOT NULL CHECK (version >= 1),
            updated_by          text NOT NULL,
            updated_at          timestamptz NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE energy.tariffs")
    op.execute("DROP TABLE energy.rollup_cursor")
    op.execute("DROP TABLE energy.hourly")
