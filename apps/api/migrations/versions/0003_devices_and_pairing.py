"""Device registry, pairing codes and digital twins.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of device_protocol.DeviceKind at the time of this migration.
KINDS = (
    "('light', 'plug', 'thermostat', 'lock', 'motion_sensor', 'contact_sensor', "
    "'climate_sensor', 'energy_meter', 'camera')"
)


def upgrade() -> None:
    # home_id is a plain uuid: homes live in the identity schema and modules never
    # share foreign keys (ADR 0001). Ownership is checked through identity.public.
    op.execute(
        f"""
        CREATE TABLE devices.devices (
            id                  text PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9_-]{{1,64}}$'),
            home_id             uuid NOT NULL,
            kind                text NOT NULL CHECK (kind IN {KINDS}),
            name                text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
            room                text CHECK (char_length(room) <= 40),
            firmware            text CHECK (char_length(firmware) <= 40),
            status              text NOT NULL CHECK (status IN ('active', 'quarantined', 'revoked')),
            status_reason       text CHECK (char_length(status_reason) <= 200),
            status_changed_at   timestamptz NOT NULL,
            paired_by           text NOT NULL,
            paired_at           timestamptz NOT NULL,
            online              boolean NOT NULL DEFAULT false,
            presence_changed_at timestamptz,
            last_seen_at        timestamptz
        )
        """
    )
    op.execute("CREATE INDEX devices_by_home ON devices.devices (home_id) WHERE status <> 'revoked'")

    op.execute(
        f"""
        CREATE TABLE devices.pairing_codes (
            id                 uuid PRIMARY KEY,
            home_id            uuid NOT NULL,
            code_hash          bytea NOT NULL UNIQUE,
            created_by         text NOT NULL,
            created_at         timestamptz NOT NULL,
            expires_at         timestamptz NOT NULL,
            name               text CHECK (char_length(name) BETWEEN 1 AND 80),
            room               text CHECK (char_length(room) <= 40),
            kind               text CHECK (kind IN {KINDS}),
            claimed_at         timestamptz,
            claimed_device_id  text REFERENCES devices.devices (id),
            CONSTRAINT claim_is_complete
                CHECK ((claimed_at IS NULL) = (claimed_device_id IS NULL)),
            CONSTRAINT expires_after_creation CHECK (expires_at > created_at)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE devices.twins (
            device_id     text PRIMARY KEY REFERENCES devices.devices (id),
            desired       jsonb NOT NULL DEFAULT '{}'::jsonb,
            desired_at    timestamptz,
            reported      jsonb NOT NULL DEFAULT '{}'::jsonb,
            reported_at   timestamptz,
            CONSTRAINT objects CHECK (
                jsonb_typeof(desired) = 'object' AND jsonb_typeof(reported) = 'object'
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE devices.twins")
    op.execute("DROP TABLE devices.pairing_codes")
    op.execute("DROP TABLE devices.devices")
