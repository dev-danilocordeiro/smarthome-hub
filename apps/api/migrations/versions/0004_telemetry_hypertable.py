"""Telemetry hypertable, columnstore compression and hierarchical continuous aggregates.

Retention is not set here: it is configuration (SMARTHOME_TELEMETRY_*_RETENTION_DAYS)
and the ingestor applies it idempotently at startup. See ADR 0007.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Long ("narrow") format: one row per metric. Devices of different kinds report
    # different metrics; a wide table would be mostly NULLs and need a migration per
    # new metric. home_id is denormalised so per-home queries never join.
    op.execute(
        """
        CREATE TABLE telemetry.readings (
            time         timestamptz NOT NULL,
            home_id      uuid NOT NULL,
            device_id    text NOT NULL,
            metric       text NOT NULL,
            value        double precision NOT NULL,
            message_id   text NOT NULL,
            received_at  timestamptz NOT NULL,
            -- Dedup guard (QoS 1 redelivery, device retries). Must include the time
            -- column, which partitions the hypertable.
            CONSTRAINT readings_once UNIQUE (device_id, metric, time)
        )
        """
    )
    op.execute("SELECT create_hypertable('telemetry.readings', by_range('time', INTERVAL '1 day'))")
    op.execute("CREATE INDEX readings_by_home ON telemetry.readings (home_id, time DESC)")
    # Columnstore: chunks older than 7 days become compressed, segmented the way they are
    # read (one device, one metric, a time range).
    op.execute(
        """
        ALTER TABLE telemetry.readings SET (
            timescaledb.enable_columnstore = true,
            timescaledb.segmentby = 'device_id, metric',
            timescaledb.orderby = 'time DESC'
        )
        """
    )
    op.execute("CALL add_columnstore_policy('telemetry.readings', after => INTERVAL '7 days')")

    # Continuous aggregates cannot be created inside a transaction.
    with op.get_context().autocommit_block():
        # Store sum + count rather than avg so coarser levels can be built from finer
        # ones without averaging averages. `materialized_only = false` serves the most
        # recent, not-yet-materialised window from raw data (real-time aggregation).
        op.execute(
            """
            CREATE MATERIALIZED VIEW telemetry.readings_1m
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT time_bucket(INTERVAL '1 minute', time) AS bucket, home_id, device_id, metric,
                   sum(value) AS total, count(*) AS samples,
                   min(value) AS min, max(value) AS max, last(value, time) AS last
            FROM telemetry.readings
            GROUP BY bucket, home_id, device_id, metric
            WITH NO DATA
            """
        )
        for name, source, width in (
            ("readings_1h", "readings_1m", "1 hour"),
            ("readings_1d", "readings_1h", "1 day"),
        ):
            op.execute(
                f"""
                CREATE MATERIALIZED VIEW telemetry.{name}
                WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
                SELECT time_bucket(INTERVAL '{width}', bucket) AS bucket,
                       home_id, device_id, metric,
                       sum(total) AS total, sum(samples) AS samples,
                       min(min) AS min, max(max) AS max, last(last, bucket) AS last
                FROM telemetry.{source}
                GROUP BY 1, home_id, device_id, metric
                WITH NO DATA
                """  # noqa: S608 - names and widths are literals from the tuple above
            )
        for name, start, end, every in (
            ("readings_1m", "2 hours", "1 minute", "1 minute"),
            ("readings_1h", "3 days", "1 hour", "15 minutes"),
            ("readings_1d", "35 days", "1 day", "1 hour"),
        ):
            op.execute(
                f"SELECT add_continuous_aggregate_policy('telemetry.{name}', "
                f"start_offset => INTERVAL '{start}', end_offset => INTERVAL '{end}', "
                f"schedule_interval => INTERVAL '{every}')"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW telemetry.readings_1d")
        op.execute("DROP MATERIALIZED VIEW telemetry.readings_1h")
        op.execute("DROP MATERIALIZED VIEW telemetry.readings_1m")
    op.execute("DROP TABLE telemetry.readings")
