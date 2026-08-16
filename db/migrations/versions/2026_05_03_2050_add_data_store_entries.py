# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add data_store_entries table for PersistentDataStore.

Revision ID: b002_data_store_entries
Revises: b001_initial_schema
Create Date: 2026-05-03 20:50:00 UTC

This second migration backs the Phase-1 PersistentDataStore subclass.
The table is tenant-scoped and RLS-policed exactly like the initial
domain tables — no special-cased exception. Phase 1 implements and
tests the table; ``get_data_store()`` keeps returning the in-memory
implementation. Phase 2 will switch the factory.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b002_data_store_entries"
down_revision: str | None = "b001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_store_entries",
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
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("data", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column(
            "meta",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.UniqueConstraint("tenant_id", "name", name="uq_data_store_entries_tenant_name"),
    )
    op.create_index("ix_data_store_entries_tenant_id", "data_store_entries", ["tenant_id"])

    # Apply the standard tenant_isolation policy via the helper from
    # the initial migration. Variants are forbidden without an ADR.
    op.execute("SELECT apply_tenant_rls('data_store_entries');")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON data_store_entries;")
    op.execute("ALTER TABLE data_store_entries NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE data_store_entries DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_data_store_entries_tenant_id", table_name="data_store_entries")
    op.drop_table("data_store_entries")
