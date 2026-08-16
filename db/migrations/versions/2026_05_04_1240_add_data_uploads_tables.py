# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add data_uploads and data_upload_sheets tables for the web Excel-import path.

Revision ID: b004_add_data_uploads_tables
Revises: b003_add_auth_columns_and_tables
Create Date: 2026-05-04 12:36:30 UTC

This migration introduces the Phase-2 (sub-stream 2d) tables that back
the FastAPI Excel-import endpoint, per ADR-0041 §3 ("Excel-import write
paths during Phase 2/3"). The tables are deliberately the *minimum
viable* representation: each upload becomes one parent row in
``data_uploads`` plus one child row per sheet in ``data_upload_sheets``,
where each sheet's DataFrame is stored as a JSONB blob in the
``DataFrame.to_dict('split')`` shape. The normalised investment-domain
schema is Phase-4 work and is **not** introduced here.

Both tables are tenant-scoped per ADR-0035: ``tenant_id`` is required
on every row, the standard ``apply_tenant_rls(...)`` policy is applied,
and the audit trigger from b001 is attached so every INSERT (and any
future UPDATE / DELETE) lands in ``audit_log``. ``data_upload_sheets``
denormalises ``tenant_id`` rather than relying on a JOIN against
``data_uploads``: ADR-0035 §3 mandates that every domain table carry
``tenant_id`` directly so RLS policies evaluate row-locally without
joins.

A unique constraint on ``(tenant_id, file_hash)`` provides idempotent
re-uploads at the API layer — re-submitting the same workbook returns
the existing record rather than creating a duplicate.

A restrictive ``WITH CHECK`` policy enforces that ``uploaded_by``
matches ``app.user_id``, defending against a route handler that
forgets to derive ``uploaded_by`` from the authenticated session. The
restrictive policy AND-combines with the standard tenant_isolation
permissive policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b004_add_data_uploads_tables"
down_revision: str | None = "b003_add_auth_columns_and_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- data_uploads ------------------------------------------------------
    op.create_table(
        "data_uploads",
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
            "uploaded_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("file_hash", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("format_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("tenant_id", "file_hash", name="uq_data_uploads_tenant_file_hash"),
    )
    op.create_index("ix_data_uploads_tenant_id", "data_uploads", ["tenant_id"])
    op.create_index(
        "ix_data_uploads_tenant_created",
        "data_uploads",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # Standard tenant-isolation policy via the b001 helper.
    op.execute("SELECT apply_tenant_rls('data_uploads');")

    # Restrictive policy: uploaded_by must match the authenticated
    # actor. AND-combines with the permissive tenant_isolation policy
    # so a route handler that forgets to derive uploaded_by from the
    # session is caught at the database boundary.
    op.execute(
        """
        CREATE POLICY data_uploads_uploaded_by_self ON data_uploads
            AS RESTRICTIVE
            FOR INSERT
            WITH CHECK (
                uploaded_by = current_setting('app.user_id')::uuid
            );
        """
    )

    # Audit trigger — fires INSERT/UPDATE/DELETE; uploads are
    # immutable in Phase 2 so UPDATE/DELETE will not normally fire.
    op.execute(
        """
        CREATE TRIGGER data_uploads_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON data_uploads
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- data_upload_sheets ------------------------------------------------
    op.create_table(
        "data_upload_sheets",
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
            "upload_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_uploads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sheet_name", sa.Text(), nullable=False),
        sa.Column("data", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("column_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("upload_id", "sheet_name", name="uq_data_upload_sheets_upload_sheet"),
    )
    op.create_index("ix_data_upload_sheets_tenant_id", "data_upload_sheets", ["tenant_id"])
    op.create_index("ix_data_upload_sheets_upload_id", "data_upload_sheets", ["upload_id"])

    op.execute("SELECT apply_tenant_rls('data_upload_sheets');")

    op.execute(
        """
        CREATE TRIGGER data_upload_sheets_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON data_upload_sheets
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS data_upload_sheets_audit_trigger ON data_upload_sheets;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON data_upload_sheets;")
    op.execute("ALTER TABLE data_upload_sheets NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE data_upload_sheets DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_data_upload_sheets_upload_id", table_name="data_upload_sheets")
    op.drop_index("ix_data_upload_sheets_tenant_id", table_name="data_upload_sheets")
    op.drop_table("data_upload_sheets")

    op.execute("DROP TRIGGER IF EXISTS data_uploads_audit_trigger ON data_uploads;")
    op.execute("DROP POLICY IF EXISTS data_uploads_uploaded_by_self ON data_uploads;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON data_uploads;")
    op.execute("ALTER TABLE data_uploads NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE data_uploads DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_data_uploads_tenant_created", table_name="data_uploads")
    op.drop_index("ix_data_uploads_tenant_id", table_name="data_uploads")
    op.drop_table("data_uploads")
