# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add the case-workflow tables: cases, case_entries, case_attachments.

Revision ID: b031_add_case_workflow
Revises: b030_drop_portfolio_aum
Create Date: 2026-07-21 12:00:00 UTC

The persistence layer for the Cases area (ADR-0107). Three tenant-scoped
tables land together: ``cases`` (a unit of decision work — opened
manually or from an Irene finding, closed with a mandatory note),
``case_entries`` (the append-only timeline, one row per situation, with a
JSONB ``payload`` opaque to persistence) and ``case_attachments`` (in-
database file bytes addressed only through their pin entry). All three are
tenant-scoped (ADR-0035): ``tenant_id`` is required on every row and the
standard ``apply_tenant_rls(...)`` policy is applied to each table. No
audit triggers are installed (the b019 idiom). No SQL enums are used for
the ``state`` / ``kind`` / ``actor`` vocabularies — the canonical values
are enforced in application code, matching the codebase's TEXT-for-status
convention.

ADR-0107 §2 names the entities ``case`` / ``case_entry``; the tables are
plural per the Gate-C0 decision — ``case`` is a reserved SQL keyword, and
the plural forms follow the majority convention already in the schema
(``users``, ``tenants``, ``data_uploads``, ``fx_rates``).

The migration is fully reversible: ``downgrade`` drops the three tables in
FK-safe order (``case_attachments``, ``case_entries``, ``cases``).
Postgres drops each table's RLS policy and row-security state together
with the table, so no explicit policy drop is required.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b031_add_case_workflow"
down_revision: str | None = "b030_drop_portfolio_aum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- cases ------------------------------------------------------------
    op.create_table(
        "cases",
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
        sa.Column("case_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        # References the finding, never mutates it (ADR-0085); nullable for
        # manually opened cases.
        sa.Column(
            "finding_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("irene_finding.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "opened_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "closed_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_note", sa.Text(), nullable=True),
        # Race-safety guarantee for the C1b tenant-sequential number allocation.
        sa.UniqueConstraint(
            "tenant_id",
            "case_number",
            name="uq_cases_tenant_case_number",
        ),
    )

    op.execute("SELECT apply_tenant_rls('cases');")

    # ---- case_entries -----------------------------------------------------
    op.create_table(
        "case_entries",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Denormalised from the parent cases row for row-local RLS (ADR-0035 §3).
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        # Set where a user acted; null for system-authored entries.
        sa.Column(
            "actor_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # The per-kind timeline contract; opaque to persistence.
        sa.Column(
            "payload",
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
    )

    op.create_index(
        "ix_case_entries_case_id",
        "case_entries",
        ["case_id"],
    )
    op.execute("SELECT apply_tenant_rls('case_entries');")

    # ---- case_attachments -------------------------------------------------
    op.create_table(
        "case_attachments",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Denormalised from the parent cases row for row-local RLS (ADR-0035 §3).
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        # The file bytes (BYTEA). No (tenant_id, sha256) unique constraint:
        # the same document pinned in two cases is stored twice by design.
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "uploaded_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    op.create_index(
        "ix_case_attachments_case_id",
        "case_attachments",
        ["case_id"],
    )
    op.execute("SELECT apply_tenant_rls('case_attachments');")

    # The open list and the closed list are the two hot queries.
    op.create_index(
        "ix_cases_tenant_state",
        "cases",
        ["tenant_id", "state"],
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # FK-safe order: children before the parent. Postgres drops each table's
    # RLS policy, row-security state and indexes together with the table, so
    # no explicit policy or index drop is required.
    op.drop_table("case_attachments")
    op.drop_table("case_entries")
    op.drop_table("cases")
