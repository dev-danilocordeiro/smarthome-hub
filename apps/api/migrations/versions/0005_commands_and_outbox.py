"""Device commands and the transactional outbox.

The outbox lives in its own schema because it belongs to the shared kernel, not to a
module: any module writes to it inside its own transaction and the worker relays it to
the broker. See ADR 0009.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies at the time of this migration.
KINDS = (
    "('light', 'plug', 'thermostat', 'lock', 'motion_sensor', 'contact_sensor', "
    "'climate_sensor', 'energy_meter', 'camera')"
)
ACTIONS = "('set_state', 'identify', 'reboot')"
STATUSES = "('pending', 'delivered', 'acknowledged', 'failed', 'timed_out')"
TERMINAL = "('acknowledged', 'failed', 'timed_out')"


def upgrade() -> None:
    # device_id and home_id are plain values: devices live in another module's schema
    # and modules never share foreign keys (ADR 0001).
    op.execute(
        f"""
        CREATE TABLE commands.commands (
            id               uuid PRIMARY KEY,
            home_id          uuid NOT NULL,
            device_id        text NOT NULL,
            device_kind      text NOT NULL CHECK (device_kind IN {KINDS}),
            action           text NOT NULL CHECK (action IN {ACTIONS}),
            desired          jsonb,
            status           text NOT NULL CHECK (status IN {STATUSES}),
            issued_by        text NOT NULL,
            issued_at        timestamptz NOT NULL,
            expires_at       timestamptz NOT NULL,
            delivered_at     timestamptz,
            completed_at     timestamptz,
            reason           text CHECK (char_length(reason) <= 200),
            trace_id         text CHECK (trace_id ~ '^[0-9a-f]{{32}}$'),
            CONSTRAINT expires_after_issue CHECK (expires_at > issued_at),
            CONSTRAINT desired_only_for_set_state CHECK (
                (action = 'set_state') = (desired IS NOT NULL)
                AND (desired IS NULL OR jsonb_typeof(desired) = 'object')
            ),
            CONSTRAINT timestamps_match_status CHECK (
                CASE status
                    WHEN 'pending' THEN delivered_at IS NULL AND completed_at IS NULL
                    WHEN 'delivered' THEN delivered_at IS NOT NULL AND completed_at IS NULL
                    ELSE completed_at IS NOT NULL
                END
            )
        )
        """
    )
    op.execute("CREATE INDEX commands_by_device ON commands.commands (device_id, issued_at DESC)")
    # What the timeout sweeper scans: small, because commands settle within seconds.
    op.execute(
        "CREATE INDEX commands_open ON commands.commands (expires_at)"
        " WHERE status IN ('pending', 'delivered')"
    )
    # Late or duplicated acks must never rewrite history: once a command has settled it
    # stays settled, and what was asked for (and by whom) never changes.
    op.execute(
        f"""
        CREATE FUNCTION commands.guard_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'commands are kept as history (delete of % rejected)', OLD.id;
            END IF;
            IF (NEW.id, NEW.home_id, NEW.device_id, NEW.device_kind, NEW.action,
                NEW.desired, NEW.issued_by, NEW.issued_at, NEW.expires_at)
               IS DISTINCT FROM
               (OLD.id, OLD.home_id, OLD.device_id, OLD.device_kind, OLD.action,
                OLD.desired, OLD.issued_by, OLD.issued_at, OLD.expires_at) THEN
                RAISE EXCEPTION 'command % is immutable except for its outcome', OLD.id;
            END IF;
            IF OLD.status IN {TERMINAL} AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'command % already settled as %', OLD.id, OLD.status;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER commands_guard_transition BEFORE UPDATE OR DELETE ON commands.commands"
        " FOR EACH ROW EXECUTE FUNCTION commands.guard_transition()"
    )

    op.execute("CREATE SCHEMA outbox")
    op.execute(
        """
        CREATE TABLE outbox.messages (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            topic            text NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 256),
            qos              smallint NOT NULL CHECK (qos IN (0, 1, 2)),
            payload          jsonb NOT NULL,
            headers          jsonb NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(headers) = 'object'),
            created_at       timestamptz NOT NULL,
            expires_at       timestamptz,
            next_attempt_at  timestamptz NOT NULL,
            attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            last_error       text CHECK (char_length(last_error) <= 500),
            -- 'expired': its deadline passed before the broker could take it, so it was
            -- dropped instead of delivering a command the device must reject anyway.
            outcome          text CHECK (outcome IN ('published', 'expired')),
            processed_at     timestamptz,
            CONSTRAINT processed_once CHECK ((outcome IS NULL) = (processed_at IS NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX messages_pending ON outbox.messages (next_attempt_at, id)"
        " WHERE processed_at IS NULL"
    )
    op.execute(
        "CREATE INDEX messages_processed ON outbox.messages (processed_at)"
        " WHERE processed_at IS NOT NULL"
    )
    # Wakes the relay as soon as a transaction that wrote to the outbox commits (NOTIFY
    # is delivered on commit only). Polling remains the fallback for missed signals.
    op.execute(
        """
        CREATE FUNCTION outbox.notify_relay() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('outbox', '');
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER messages_notify AFTER INSERT ON outbox.messages"
        " FOR EACH STATEMENT EXECUTE FUNCTION outbox.notify_relay()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE outbox.messages")
    op.execute("DROP FUNCTION outbox.notify_relay()")
    op.execute("DROP SCHEMA outbox RESTRICT")
    op.execute("DROP TABLE commands.commands")
    op.execute("DROP FUNCTION commands.guard_transition()")
