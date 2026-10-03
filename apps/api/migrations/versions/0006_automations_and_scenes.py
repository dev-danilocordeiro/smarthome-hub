"""Automations, their revisions, trigger state, schedules, runs; scenes.

See ADR 0010.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies at the time of this migration.
RUN_STATUSES = "('running', 'completed', 'failed', 'skipped', 'suppressed')"


def upgrade() -> None:
    # home_id and device ids are plain values: modules never share foreign keys (ADR 0001).
    op.execute(
        """
        CREATE TABLE automations.automations (
            id             uuid PRIMARY KEY,
            home_id        uuid NOT NULL,
            name           text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
            description    text CHECK (char_length(description) <= 500),
            enabled        boolean NOT NULL,
            status         text NOT NULL CHECK (status IN ('active', 'suspended')),
            status_reason  text CHECK (char_length(status_reason) <= 300),
            version        integer NOT NULL CHECK (version >= 1),
            definition     jsonb NOT NULL CHECK (
                jsonb_typeof(definition) = 'object' AND definition->>'schema_version' = '1'
            ),
            timezone       text NOT NULL,
            created_by     text NOT NULL,
            created_at     timestamptz NOT NULL,
            updated_by     text NOT NULL,
            updated_at     timestamptz NOT NULL,
            CONSTRAINT suspended_has_reason CHECK (
                (status = 'suspended') = (status_reason IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX automations_name_per_home"
        " ON automations.automations (home_id, lower(name))"
    )

    # Every version ever saved, including the last one before a delete. Never rewritten.
    op.execute(
        """
        CREATE TABLE automations.revisions (
            automation_id  uuid NOT NULL,
            version        integer NOT NULL CHECK (version >= 1),
            home_id        uuid NOT NULL,
            name           text NOT NULL,
            definition     jsonb NOT NULL,
            change         text NOT NULL CHECK (change IN ('created', 'updated', 'deleted')),
            changed_by     text NOT NULL,
            changed_at     timestamptz NOT NULL,
            PRIMARY KEY (automation_id, version)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION automations.reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER revisions_append_only BEFORE UPDATE OR DELETE ON automations.revisions"
        " FOR EACH ROW EXECUTE FUNCTION automations.reject_mutation()"
    )

    # What each device trigger last saw (edge detection) and its pending hold timer.
    op.execute(
        """
        CREATE TABLE automations.trigger_states (
            automation_id  uuid NOT NULL,
            trigger_index  smallint NOT NULL CHECK (trigger_index >= 0),
            matched        boolean,
            observed_at    timestamptz,
            fire_at        timestamptz,
            PRIMARY KEY (automation_id, trigger_index),
            CONSTRAINT timer_only_while_matching CHECK (fire_at IS NULL OR matched)
        )
        """
    )
    op.execute(
        "CREATE INDEX trigger_states_due ON automations.trigger_states (fire_at)"
        " WHERE fire_at IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE automations.schedules (
            automation_id  uuid NOT NULL,
            trigger_index  smallint NOT NULL CHECK (trigger_index >= 0),
            next_at        timestamptz NOT NULL,
            PRIMARY KEY (automation_id, trigger_index)
        )
        """
    )
    op.execute("CREATE INDEX schedules_due ON automations.schedules (next_at)")

    op.execute(
        f"""
        CREATE TABLE automations.runs (
            id                  uuid PRIMARY KEY,
            automation_id       uuid NOT NULL,
            home_id             uuid NOT NULL,
            automation_version  integer NOT NULL,
            trigger_index       smallint NOT NULL CHECK (trigger_index >= 0),
            event_key           text NOT NULL CHECK (char_length(event_key) <= 200),
            status              text NOT NULL CHECK (status IN {RUN_STATUSES}),
            depth               integer NOT NULL CHECK (depth >= 0),
            started_at          timestamptz NOT NULL,
            finished_at         timestamptz,
            reason              text CHECK (char_length(reason) <= 300),
            outcomes            jsonb NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(outcomes) = 'array'),
            trace_id            text CHECK (trace_id ~ '^[0-9a-f]{{32}}$'),
            CONSTRAINT finished_unless_running CHECK (
                (status = 'running') = (finished_at IS NULL)
            ),
            -- The same event, timer or schedule slot runs an automation at most once,
            -- however often it is redelivered.
            CONSTRAINT one_run_per_event UNIQUE (automation_id, event_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX runs_by_automation ON automations.runs (automation_id, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX runs_running ON automations.runs (started_at) WHERE status = 'running'"
    )
    op.execute("CREATE INDEX runs_by_age ON automations.runs (started_at)")
    # A run's outcome is written once. Deletes are retention purges.
    op.execute(
        """
        CREATE FUNCTION automations.guard_run() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'running' THEN
                RAISE EXCEPTION 'run % already finished as %', OLD.id, OLD.status;
            END IF;
            IF (NEW.id, NEW.automation_id, NEW.event_key, NEW.started_at)
               IS DISTINCT FROM (OLD.id, OLD.automation_id, OLD.event_key, OLD.started_at) THEN
                RAISE EXCEPTION 'run % is immutable except for its outcome', OLD.id;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER runs_guard BEFORE UPDATE ON automations.runs"
        " FOR EACH ROW EXECUTE FUNCTION automations.guard_run()"
    )

    op.execute(
        """
        CREATE TABLE automations.scenes (
            id          uuid PRIMARY KEY,
            home_id     uuid NOT NULL,
            name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
            states      jsonb NOT NULL CHECK (
                jsonb_typeof(states) = 'array' AND jsonb_array_length(states) BETWEEN 1 AND 50
            ),
            version     integer NOT NULL CHECK (version >= 1),
            created_by  text NOT NULL,
            created_at  timestamptz NOT NULL,
            updated_by  text NOT NULL,
            updated_at  timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX scenes_name_per_home ON automations.scenes (home_id, lower(name))"
    )


def downgrade() -> None:
    op.execute("DROP TABLE automations.scenes")
    op.execute("DROP TABLE automations.runs")
    op.execute("DROP FUNCTION automations.guard_run()")
    op.execute("DROP TABLE automations.schedules")
    op.execute("DROP TABLE automations.trigger_states")
    op.execute("DROP TABLE automations.revisions")
    op.execute("DROP FUNCTION automations.reject_mutation()")
    op.execute("DROP TABLE automations.automations")
