"""Identity tables (homes, memberships, invitations, users) and the append-only audit log.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = "('owner', 'resident', 'guest', 'viewer')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE identity.users (
            subject        text PRIMARY KEY,
            email          text,
            display_name   text,
            first_seen_at  timestamptz NOT NULL,
            last_login_at  timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.homes (
            id          uuid PRIMARY KEY,
            name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
            timezone    text NOT NULL,
            created_by  text NOT NULL,
            created_at  timestamptz NOT NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE identity.memberships (
            id            uuid PRIMARY KEY,
            home_id       uuid NOT NULL REFERENCES identity.homes (id),
            user_id       text NOT NULL,
            role          text NOT NULL CHECK (role IN {ROLES}),
            granted_by    text NOT NULL,
            granted_at    timestamptz NOT NULL,
            expires_at    timestamptz,
            device_scope  text[],
            revoked_at    timestamptz,
            revoked_by    text,
            -- Guests always expire; only guests can be limited to a set of devices;
            -- owners never expire.
            CONSTRAINT guest_access_expires
                CHECK (role <> 'guest' OR expires_at IS NOT NULL),
            CONSTRAINT device_scope_only_for_guests
                CHECK (device_scope IS NULL OR role = 'guest'),
            CONSTRAINT owners_do_not_expire
                CHECK (role <> 'owner' OR expires_at IS NULL),
            CONSTRAINT revocation_is_complete
                CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
        )
        """
    )
    # History is kept (revoked rows stay); at most one live membership per user and home.
    op.execute(
        """
        CREATE UNIQUE INDEX memberships_one_active_per_user
            ON identity.memberships (home_id, user_id) WHERE revoked_at IS NULL
        """
    )
    op.execute(
        "CREATE INDEX memberships_by_user ON identity.memberships (user_id) "
        "WHERE revoked_at IS NULL"
    )
    op.execute(
        f"""
        CREATE TABLE identity.invitations (
            id                       uuid PRIMARY KEY,
            home_id                  uuid NOT NULL REFERENCES identity.homes (id),
            token_hash               bytea NOT NULL UNIQUE,
            role                     text NOT NULL CHECK (role IN {ROLES} AND role <> 'owner'),
            invited_by               text NOT NULL,
            created_at               timestamptz NOT NULL,
            expires_at               timestamptz NOT NULL,
            guest_access_expires_at  timestamptz,
            device_scope             text[],
            accepted_by              text,
            accepted_at              timestamptz,
            revoked_at               timestamptz,
            CONSTRAINT guest_invitation_sets_access_expiry
                CHECK ((role = 'guest') = (guest_access_expires_at IS NOT NULL)),
            CONSTRAINT device_scope_only_for_guests
                CHECK (device_scope IS NULL OR role = 'guest'),
            CONSTRAINT acceptance_is_complete
                CHECK ((accepted_at IS NULL) = (accepted_by IS NULL)),
            CONSTRAINT accepted_or_revoked_not_both
                CHECK (accepted_at IS NULL OR revoked_at IS NULL)
        )
        """
    )

    # --- Audit log -------------------------------------------------------------------
    # Not owned by a business module: every module appends to it inside its own
    # transaction. Rows are chained per tenant (sha256 over the previous hash and the
    # canonical entry), so a rewrite by someone who bypasses the trigger is detectable.
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")
    op.execute(
        """
        CREATE TABLE audit.entries (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id    uuid,
            occurred_at  timestamptz NOT NULL,
            actor        text NOT NULL,
            action       text NOT NULL,
            target_type  text NOT NULL,
            target_id    text NOT NULL,
            details      jsonb NOT NULL DEFAULT '{}'::jsonb,
            trace_id     text,
            prev_hash    bytea,
            hash         bytea NOT NULL UNIQUE,
            CONSTRAINT sha256_lengths CHECK (
                octet_length(hash) = 32 AND (prev_hash IS NULL OR octet_length(prev_hash) = 32)
            )
        )
        """
    )
    op.execute("CREATE INDEX entries_by_tenant ON audit.entries (tenant_id, id)")
    # One successor per link: two writers racing on the same tenant cannot both extend
    # the chain from the same row (the advisory lock prevents it; this proves it).
    op.execute(
        "CREATE UNIQUE INDEX entries_single_successor ON audit.entries "
        "(coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), prev_hash) "
        "NULLS NOT DISTINCT"
    )
    op.execute(
        """
        CREATE FUNCTION audit.reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit.entries is append-only (% rejected)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER entries_append_only
            BEFORE UPDATE OR DELETE ON audit.entries
            FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER entries_no_truncate
            BEFORE TRUNCATE ON audit.entries
            FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA audit CASCADE")
    op.execute("DROP TABLE identity.invitations")
    op.execute("DROP TABLE identity.memberships")
    op.execute("DROP TABLE identity.homes")
    op.execute("DROP TABLE identity.users")
