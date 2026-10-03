"""Alerts, inboxes, the delivery queue, preferences and webhooks.

See ADR 0013.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies at the time of this migration.
SEVERITIES = "('info', 'warning', 'critical')"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE notifications.alerts (
            id               uuid PRIMARY KEY,
            home_id          uuid NOT NULL,
            key              text NOT NULL CHECK (char_length(key) <= 200),
            kind             text NOT NULL
                             CHECK (kind IN ('device_offline', 'low_battery', 'energy_budget')),
            severity         text NOT NULL CHECK (severity IN {SEVERITIES}),
            device_id        text,
            title            text NOT NULL CHECK (char_length(title) <= 200),
            details          jsonb NOT NULL DEFAULT '{{}}',
            status           text NOT NULL CHECK (status IN ('pending', 'open', 'resolved')),
            observed_at      timestamptz NOT NULL,
            created_at       timestamptz NOT NULL,
            due_at           timestamptz,
            opened_at        timestamptz,
            resolved_at      timestamptz,
            acknowledged_by  text,
            acknowledged_at  timestamptz,
            CONSTRAINT pending_has_due CHECK ((status = 'pending') = (due_at IS NOT NULL)),
            CONSTRAINT open_was_opened CHECK (status <> 'open' OR opened_at IS NOT NULL),
            CONSTRAINT resolved_has_time CHECK ((status = 'resolved') = (resolved_at IS NOT NULL))
        )
        """
    )
    # The rule that makes alerts deduplicate under concurrency: one live alert per key.
    op.execute(
        "CREATE UNIQUE INDEX alerts_one_live_per_key ON notifications.alerts (home_id, key)"
        " WHERE status IN ('pending', 'open')"
    )
    op.execute(
        "CREATE INDEX alerts_by_key_observed"
        " ON notifications.alerts (home_id, key, observed_at DESC)"
    )
    op.execute("CREATE INDEX alerts_by_home ON notifications.alerts (home_id, created_at DESC)")
    op.execute("CREATE INDEX alerts_due ON notifications.alerts (due_at) WHERE status = 'pending'")

    op.execute(
        f"""
        CREATE TABLE notifications.inbox (
            id          uuid PRIMARY KEY,
            user_id     text NOT NULL,
            home_id     uuid NOT NULL,
            alert_id    uuid,
            event       text NOT NULL CHECK (event IN ('opened', 'resolved')),
            severity    text NOT NULL CHECK (severity IN {SEVERITIES}),
            title       text NOT NULL,
            body        text NOT NULL,
            created_at  timestamptz NOT NULL,
            read_at     timestamptz,
            CONSTRAINT once_per_person UNIQUE (alert_id, event, user_id)
        )
        """
    )
    op.execute("CREATE INDEX inbox_by_user ON notifications.inbox (user_id, created_at DESC)")
    op.execute(
        "CREATE INDEX inbox_unread ON notifications.inbox (user_id, home_id) WHERE read_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE notifications.deliveries (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            home_id          uuid NOT NULL,
            alert_id         uuid,
            user_id          text,
            channel          text NOT NULL CHECK (channel IN ('email', 'webhook')),
            target           text NOT NULL,
            payload          jsonb NOT NULL,
            idempotency_key  text NOT NULL UNIQUE,
            status           text NOT NULL
                             CHECK (status IN ('queued', 'sent', 'failed', 'skipped')),
            attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            next_attempt_at  timestamptz,
            last_error       text,
            created_at       timestamptz NOT NULL,
            settled_at       timestamptz,
            CONSTRAINT queued_has_next CHECK ((status = 'queued') = (next_attempt_at IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX deliveries_due ON notifications.deliveries (next_attempt_at)"
        " WHERE status = 'queued'"
    )
    op.execute(
        "CREATE INDEX deliveries_sent_to ON notifications.deliveries (channel, target, settled_at)"
        " WHERE status = 'sent'"
    )

    op.execute(
        f"""
        CREATE TABLE notifications.preferences (
            home_id             uuid NOT NULL,
            user_id             text NOT NULL,
            email_enabled       boolean NOT NULL,
            min_email_severity  text NOT NULL CHECK (min_email_severity IN {SEVERITIES}),
            quiet_start         smallint CHECK (quiet_start BETWEEN 0 AND 23),
            quiet_end           smallint CHECK (quiet_end BETWEEN 0 AND 23),
            updated_at          timestamptz NOT NULL,
            PRIMARY KEY (home_id, user_id),
            CONSTRAINT quiet_hours_paired CHECK ((quiet_start IS NULL) = (quiet_end IS NULL))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE notifications.webhooks (
            home_id     uuid PRIMARY KEY,
            url         text NOT NULL CHECK (char_length(url) <= 500),
            secret      text NOT NULL CHECK (char_length(secret) >= 32),
            enabled     boolean NOT NULL,
            updated_by  text NOT NULL,
            updated_at  timestamptz NOT NULL
        )
        """
    )


def downgrade() -> None:
    for table in ("webhooks", "preferences", "deliveries", "inbox", "alerts"):
        op.execute(f"DROP TABLE notifications.{table}")
