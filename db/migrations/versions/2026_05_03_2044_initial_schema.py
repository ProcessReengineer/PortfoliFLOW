# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Initial schema: tenants, users, audit_log with RLS and audit trigger.

Revision ID: b001_initial_schema
Revises:
Create Date: 2026-05-03 20:44:00 UTC

This is stream B's first domain migration. It creates the three Phase-1
domain tables, enables Row-Level Security and FORCE ROW LEVEL SECURITY
on every one of them, attaches the standard tenant-isolation policies
(per ADR-0035 §2), and wires the audit trigger that fires on writes to
``users``.

No data is inserted by this migration. The Phase-2 sentinel-tenant /
sentinel-user bootstrap is a separate workflow (ADR-0035 §8,
ADR-0036 §6) that runs against the schema this migration produces.

The migration creates one PL/pgSQL helper, ``apply_tenant_rls``, which
generates the standard tenant_isolation policy block from a table name.
Future migrations call this helper to keep policy syntax uniform — see
ADR-0035 §2 ("variants are forbidden without an ADR").
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# PL/pgSQL helpers
# ---------------------------------------------------------------------------

# Standard tenant-isolation policy generator. Called from this migration
# for users / audit_log; reused by every future migration that creates
# a domain table. The policy filters reads and visibility for
# update/delete via USING, and prevents cross-tenant inserts/updates
# via WITH CHECK (per ADR-0035 §2).
APPLY_TENANT_RLS_FUNCTION = """
CREATE OR REPLACE FUNCTION apply_tenant_rls(target_table TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target_table);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',  target_table);
    EXECUTE format(
        'CREATE POLICY tenant_isolation ON %I
            USING       (tenant_id = current_setting(''app.tenant_id'')::uuid)
            WITH CHECK  (tenant_id = current_setting(''app.tenant_id'')::uuid)',
        target_table
    );
END;
$$;
"""

DROP_APPLY_TENANT_RLS_FUNCTION = "DROP FUNCTION IF EXISTS apply_tenant_rls(TEXT);"

# Generic audit trigger. Captures INSERT/UPDATE/DELETE on the table it
# is attached to and writes a JSONB before/after row into audit_log.
# tenant_id and the record id are pulled from the affected row.
# user_id is NULL in Phase 1 (no auth yet); Phase 2 sets it from a
# session-scoped GUC the auth middleware populates (planned name:
# `app.user_id`, mirroring `app.tenant_id`).
AUDIT_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_trigger_function()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    affected_row JSONB;
    affected_id  UUID;
    old_payload  JSONB;
    new_payload  JSONB;
    affected_tenant UUID;
    actor_user   UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        affected_row := to_jsonb(OLD);
        old_payload  := affected_row;
        new_payload  := NULL;
    ELSIF TG_OP = 'UPDATE' THEN
        affected_row := to_jsonb(NEW);
        old_payload  := to_jsonb(OLD);
        new_payload  := affected_row;
    ELSE  -- INSERT
        affected_row := to_jsonb(NEW);
        old_payload  := NULL;
        new_payload  := affected_row;
    END IF;

    affected_id := (affected_row ->> 'id')::uuid;
    affected_tenant := (affected_row ->> 'tenant_id')::uuid;

    -- Phase 1: app.user_id is not set; Phase 2 will populate it from
    -- the auth-middleware. current_setting(..., true) returns NULL
    -- when the GUC is missing instead of raising.
    BEGIN
        actor_user := NULLIF(current_setting('app.user_id', true), '')::uuid;
    EXCEPTION WHEN others THEN
        actor_user := NULL;
    END;

    INSERT INTO audit_log (
        tenant_id, user_id, table_name, record_id, operation,
        old_data, new_data
    ) VALUES (
        affected_tenant, actor_user, TG_TABLE_NAME, affected_id, TG_OP,
        old_payload, new_payload
    );

    RETURN NULL;  -- AFTER triggers ignore the return value.
END;
$$;
"""

DROP_AUDIT_TRIGGER_FUNCTION = "DROP FUNCTION IF EXISTS audit_trigger_function();"


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # gen_random_uuid() lives in pgcrypto on Postgres < 13; Postgres
    # 13+ ships it natively. We're on 16, so the function is built-in
    # but still requires the extension to be enabled in some
    # distributions. CREATE EXTENSION IF NOT EXISTS is idempotent.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # ---- tenants -----------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # ---- users -------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("external_idp", sa.String(64), nullable=True),
        sa.Column("external_subject", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        sa.UniqueConstraint("external_idp", "external_subject", name="uq_users_external_identity"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    # ---- audit_log ---------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("table_name", sa.String(128), nullable=False),
        sa.Column(
            "record_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("old_data", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("new_data", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])
    op.create_index(
        "ix_audit_log_table_record",
        "audit_log",
        ["table_name", "record_id"],
    )

    # ---- helpers -----------------------------------------------------------
    op.execute(APPLY_TENANT_RLS_FUNCTION)
    op.execute(AUDIT_TRIGGER_FUNCTION)

    # ---- RLS on tenants ----------------------------------------------------
    # tenants gets a self-only visibility policy rather than the standard
    # tenant_isolation: a tenant can see and modify only its own row.
    # New tenant rows are created exclusively by the superuser path
    # (Alembic seed migrations or a future bootstrap CLI) — the
    # portfoliflow_app role cannot insert tenants because the WITH CHECK
    # condition (id = current_setting('app.tenant_id')::uuid) fails for
    # any id not equal to the active tenant context. Tenant management is
    # a Phase-5 deliverable.
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE tenants FORCE  ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY tenant_self_visibility ON tenants
            USING       (id = current_setting('app.tenant_id')::uuid)
            WITH CHECK  (id = current_setting('app.tenant_id')::uuid);
        """
    )

    # ---- RLS on users / audit_log via the helper ---------------------------
    op.execute("SELECT apply_tenant_rls('users');")
    op.execute("SELECT apply_tenant_rls('audit_log');")

    # ---- Audit trigger on users -------------------------------------------
    # Pattern is established here on the users table; future domain
    # tables get the same trigger via op.execute in their own migrations.
    op.execute(
        """
        CREATE TRIGGER users_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON users
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS users_audit_trigger ON users;")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON audit_log;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON users;")
    op.execute("DROP POLICY IF EXISTS tenant_self_visibility ON tenants;")

    op.execute("ALTER TABLE audit_log NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE users     NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE users     DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE tenants   NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE tenants   DISABLE ROW LEVEL SECURITY;")

    op.execute(DROP_AUDIT_TRIGGER_FUNCTION)
    op.execute(DROP_APPLY_TENANT_RLS_FUNCTION)

    op.drop_index("ix_audit_log_table_record", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_id", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")

    op.drop_table("tenants")

    # Note: pgcrypto is left installed — it may be in use by other
    # objects after this point and dropping it would risk collateral
    # damage. CREATE EXTENSION IF NOT EXISTS in upgrade() is idempotent.
