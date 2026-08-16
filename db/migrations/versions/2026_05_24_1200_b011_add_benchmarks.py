# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add benchmarks, benchmark_observations, asset_class_benchmark_mapping.

Revision ID: b011_add_benchmarks
Revises: b010_add_limits_aum_anlv
Create Date: 2026-05-24 12:00:00 UTC

The Phase-7 Benchmarks & Attribution data layer per ADR-0061.
Three tables land together because they form a single feature
foundation (catalogue + time series + asset-class mapping) and
share one migration scope:

1. ``benchmarks`` — per-tenant catalogue of benchmark definitions.
   ``(tenant_id, code)`` is UNIQUE; populated by the Excel import
   path (``Benchmarks actual`` sheet). Audit-tracked, RLS-scoped.
2. ``benchmark_observations`` — daily time series of period
   returns per benchmark. ``(benchmark_id, as_of_date)`` is
   UNIQUE; high-frequency table so no audit trigger is attached
   (analogous to ``investment_navs``). RLS-scoped via the
   denormalised ``tenant_id``.
3. ``asset_class_benchmark_mapping`` — many-to-many between
   asset classes and benchmarks with weights in ``[0, 1]``. In
   Phase 1 each asset class has at most one mapping with
   ``weight = 1.0``; composite-benchmark support (multiple rows
   summing to ≤ 1) is schema-ready but not exercised yet.
   Audit-tracked, RLS-scoped.

Per ADR-0061 §Decision the hard-fail-on-unknown-label discipline
is enforced at the service layer (``InvestmentService
.transform_benchmarks_from_upload``): unknown asset-class codes
or benchmark IDs in the Excel ``Benchmark Mapping`` sheet block
the import with operator-actionable messages.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b011_add_benchmarks"
down_revision: str | None = "b010_add_limits_aum_anlv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- benchmarks (per-tenant catalogue) --------------------------------
    op.create_table(
        "benchmarks",
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
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("provider_hint", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("tenant_id", "code", name="uq_benchmarks_tenant_code"),
    )
    op.execute("SELECT apply_tenant_rls('benchmarks');")
    op.execute(
        """
        CREATE TRIGGER benchmarks_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON benchmarks
        FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- benchmark_observations (daily time series) -----------------------
    op.create_table(
        "benchmark_observations",
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
            "benchmark_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("benchmarks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("period_return", sa.Numeric(20, 10), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "benchmark_id",
            "as_of_date",
            name="uq_benchmark_observations_benchmark_date",
        ),
    )
    op.create_index(
        "ix_benchmark_observations_tenant_benchmark_date",
        "benchmark_observations",
        ["tenant_id", "benchmark_id", sa.text("as_of_date DESC")],
    )
    op.execute("SELECT apply_tenant_rls('benchmark_observations');")
    # No audit trigger — high-frequency time series, analogous to
    # investment_navs (ADR-0061 §Decision).

    # ---- asset_class_benchmark_mapping (many-to-many with weights) --------
    op.create_table(
        "asset_class_benchmark_mapping",
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
            "asset_class_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("asset_classes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "benchmark_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("benchmarks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("weight", sa.Numeric(7, 4), nullable=False),
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
        sa.CheckConstraint(
            "weight >= 0 AND weight <= 1",
            name="ck_acbm_weight_range",
        ),
        sa.UniqueConstraint(
            "asset_class_id",
            "benchmark_id",
            name="uq_acbm_asset_class_benchmark",
        ),
    )
    op.create_index(
        "ix_acbm_tenant_asset_class",
        "asset_class_benchmark_mapping",
        ["tenant_id", "asset_class_id"],
    )
    op.execute("SELECT apply_tenant_rls('asset_class_benchmark_mapping');")
    op.execute(
        """
        CREATE TRIGGER acbm_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON asset_class_benchmark_mapping
        FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Drop in reverse dependency order. PostgreSQL drops policies,
    # triggers, and indexes automatically when the table is dropped.
    op.drop_table("asset_class_benchmark_mapping")
    op.drop_table("benchmark_observations")
    op.drop_table("benchmarks")
