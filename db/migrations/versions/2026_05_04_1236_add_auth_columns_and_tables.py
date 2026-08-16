# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add auth columns to ``users``, plus ``sessions`` and ``login_audit`` tables.

Revision ID: b003_add_auth_columns_and_tables
Revises: b002_data_store_entries
Create Date: 2026-05-04 12:36:00 UTC

This is sub-stream 2b's authentication schema migration. It implements
the ADR-0036 §2 user-table contract and adds the two auxiliary tables
the Phase-2 auth backend needs:

1. ``users`` is extended with ``password_hash`` (nullable, to support
   Phase-5 OIDC-only users), ``is_tenant_owner`` (the Phase-2 role
   approximation), and ``is_active`` (deactivation flag without delete).
   The CHECK constraint from ADR-0036 §2 enforces that every active user
   is authenticatable through at least one configured backend.

   The legacy ``UNIQUE (external_idp, external_subject)`` constraint
   from b001 is replaced with a partial unique index that fires only
   when both columns are non-NULL. Postgres treats NULLs as distinct
   in plain UNIQUE constraints (SQL standard), so multiple local-only
   users with both columns NULL did not collide before — but the
   constraint expressed the wrong intent. The partial index expresses
   the actual rule: "the OIDC subject is unique among rows that have
   one". This is the natural place to do the reshape (the auth
   migration owns the surrounding semantics) and avoids an index-only
   migration whose entire purpose would be cosmetic.

2. ``sessions`` is the server-side session store (per ADR-0036 §1).
   Tenant-scoped per ADR-0035, RLS-policed via the standard helper.
   ``session_token`` and ``csrf_token`` are random 256-bit values,
   base64url-encoded, stored in plaintext. They are bearer tokens
   protected by rotation, not by hashing — OWASP session-management
   guidance is explicit on this distinction. Indexed
   ``(tenant_id, user_id)`` for the per-user logout-everywhere path
   and ``(expires_at)`` for the cleanup sweep that Phase-3 introduces.

3. ``login_audit`` is the immutable record of every login attempt
   (per ADR-0036 §8). ``tenant_id`` and ``user_id`` are nullable
   because failed-login records must capture attempts where neither
   tenant nor user could be resolved. RLS uses a slightly different
   policy: the standard ``USING`` clause hides NULL-tenant rows from
   tenant-scoped reads, but ``WITH CHECK`` permits insertion of
   NULL-tenant rows so the auth backend can record unrecognised-email
   attempts. NULL-tenant rows are visible only to a future system
   administrator role (Phase 5).

   In Phase 2 the auth backend writes ``login_audit`` rows via the
   superuser engine (``DATABASE_URL_SUPERUSER``) — see
   ``services/auth/local_password.py``. The asymmetry is deliberate:
   at the moment of a failed login attempt the application does not
   yet have a trusted ``app.tenant_id``, so a tenant-scoped session
   cannot exist. The superuser engine bypasses RLS and can insert
   NULL-tenant rows. A regression test asserts the engine is used
   only for ``login_audit`` writes.

The audit trigger from b001 is re-applied to ``users`` automatically
(it is attached to the table, not to specific columns) — adding new
columns to ``users`` does not require re-attaching the trigger.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b003_add_auth_columns_and_tables"
down_revision: str | None = "b002_data_store_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- users: new columns ------------------------------------------------
    op.add_column(
        "users",
        sa.Column("password_hash", sa.Text(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_tenant_owner",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
    )

    # ---- users: clean up unauthenticatable Phase-2a rows -------------------
    # Sub-stream 2a's bootstrap inserted user rows with NULL
    # password_hash because the column did not yet exist. Those rows
    # are not authenticatable through any backend (no password, no
    # OIDC subject) — they are orphans from the stub bootstrap. The
    # CHECK constraint we are about to add would reject them. The
    # operator's expected next step after this migration is
    # ``portfoliflow bootstrap`` (which now persists the hash), so
    # cleanup here is the right answer rather than a constraint
    # workaround.
    op.execute(
        """
        DELETE FROM users
        WHERE password_hash IS NULL
          AND (external_idp IS NULL OR external_subject IS NULL)
        """
    )

    # ---- users: CHECK constraint (ADR-0036 §2) -----------------------------
    op.create_check_constraint(
        "ck_users_authenticatable",
        "users",
        (
            "password_hash IS NOT NULL "
            "OR (external_idp IS NOT NULL AND external_subject IS NOT NULL)"
        ),
    )

    # ---- users: replace UNIQUE with partial unique index -------------------
    op.drop_constraint("uq_users_external_identity", "users", type_="unique")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_users_external_identity
            ON users (external_idp, external_subject)
            WHERE external_idp IS NOT NULL
              AND external_subject IS NOT NULL
        """
    )

    # ---- sessions ----------------------------------------------------------
    op.create_table(
        "sessions",
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
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_token", sa.Text(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "ip_address",
            sa.dialects.postgresql.INET(),
            nullable=True,
        ),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.UniqueConstraint("session_token", name="uq_sessions_session_token"),
    )
    op.create_index("ix_sessions_tenant_user", "sessions", ["tenant_id", "user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    # Standard tenant-isolation policy via the b001 helper.
    op.execute("SELECT apply_tenant_rls('sessions');")

    # ---- login_audit -------------------------------------------------------
    op.create_table(
        "login_audit",
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
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("email_attempted", sa.Text(), nullable=False),
        sa.Column(
            "ip_address",
            sa.dialects.postgresql.INET(),
            nullable=True,
        ),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_login_audit_tenant_created",
        "login_audit",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_login_audit_email_created",
        "login_audit",
        ["email_attempted", sa.text("created_at DESC")],
    )

    # login_audit gets a custom RLS policy (not the standard helper):
    # tenant-scoped reads hide NULL-tenant rows, but inserts of
    # NULL-tenant rows are permitted so failed-tenant-resolution
    # attempts can still be recorded by the superuser engine.
    op.execute("ALTER TABLE login_audit ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE login_audit FORCE  ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON login_audit
            USING       (tenant_id = current_setting('app.tenant_id')::uuid)
            WITH CHECK  (tenant_id = current_setting('app.tenant_id')::uuid
                         OR tenant_id IS NULL);
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # ---- login_audit -------------------------------------------------------
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON login_audit;")
    op.execute("ALTER TABLE login_audit NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE login_audit DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_login_audit_email_created", table_name="login_audit")
    op.drop_index("ix_login_audit_tenant_created", table_name="login_audit")
    op.drop_table("login_audit")

    # ---- sessions ----------------------------------------------------------
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON sessions;")
    op.execute("ALTER TABLE sessions NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE sessions DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_tenant_user", table_name="sessions")
    op.drop_table("sessions")

    # ---- users -------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS uq_users_external_identity;")
    op.create_unique_constraint(
        "uq_users_external_identity",
        "users",
        ["external_idp", "external_subject"],
    )
    op.drop_constraint("ck_users_authenticatable", "users", type_="check")
    op.drop_column("users", "is_active")
    op.drop_column("users", "is_tenant_owner")
    op.drop_column("users", "password_hash")
