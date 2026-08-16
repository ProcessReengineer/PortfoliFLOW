# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add ``super_admin_audit`` table for ADR-0064 platform operations.

Revision ID: b013_super_admin_audit
Revises: b012_multi_tenant_activation
Create Date: 2026-05-26 15:00:00 UTC

Per ADR-0064 §4. ``super_admin_audit`` captures every super-admin
action — both web routes and the CLI emergency pathway
(``inspect-tenant``, ``create-tenant``, ``create-user``,
``create-super-admin``). Rows cross tenant boundaries by design:
``target_tenant_id`` may be NULL (platform-wide action), the
system tenant (super-admin self-management), or any tenant
(``inspect-tenant`` / ``create-tenant`` against a target).

RLS policy gates SELECTs on the ``app.is_super_admin`` GUC, which
``tenant_context(..., is_super_admin=True)`` sets alongside
``app.tenant_id`` and ``app.user_id``. Writes go via the audit
engine, which bypasses RLS structurally.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b013_super_admin_audit"
down_revision: str | None = "b012_multi_tenant_activation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "super_admin_audit",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "super_admin_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "target_tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "ip_address",
            sa.dialects.postgresql.INET(),
            nullable=True,
        ),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_super_admin_audit_created",
        "super_admin_audit",
        [sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_super_admin_audit_target_tenant",
        "super_admin_audit",
        ["target_tenant_id"],
        postgresql_where=sa.text("target_tenant_id IS NOT NULL"),
    )

    # Custom RLS — gate reads on app.is_super_admin GUC. Writes via
    # the audit engine bypass RLS structurally.
    op.execute("ALTER TABLE super_admin_audit ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE super_admin_audit FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY super_admin_isolation ON super_admin_audit
            USING       (current_setting('app.is_super_admin', true) = 'true')
            WITH CHECK  (current_setting('app.is_super_admin', true) = 'true');
        """
    )


def downgrade() -> None:
    # Per project convention (b010): no-op downgrade. Reset workflow
    # is the supported teardown path.
    pass
