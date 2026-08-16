# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add liquid-archetype FI tables and nav basis discriminator.

Revision ID: b016_add_liquid_archetypes
Revises: b015_add_user_display_name
Create Date: 2026-06-15 12:00:00 UTC

The per-investment data layer for the liquid-asset archetypes per
ADR-0079 §2. Three new tenant-scoped time-series tables land together
because they form the Fixed-Income archetype's reference data, plus
one additive column on the existing ``investment_navs`` table:

1. ``investment_bond_analytics`` — fixed-income characteristics
   (``ytm``, ``eff_duration``, ``oas``, ``convexity``) per investment
   per statement day. Natural key ``(investment_id, as_of_date)``.
   ``ytm`` / ``eff_duration`` NOT NULL; ``oas`` / ``convexity``
   nullable. No value/sign CHECK on the numeric columns — yields and
   spreads can be negative (EUR govvies). No ``tr_index`` column:
   total return is derived on read (ADR-0079 §3, ADR-0013).
2. ``investment_rating_weight`` — credit-rating distribution per
   investment per statement day. Natural key ``(investment_id,
   as_of_date, rating_bucket)``; ``rating_bucket`` constrained text
   over the eight canonical buckets.
3. ``investment_maturity_weight`` — maturity ladder per investment
   per statement day. Natural key ``(investment_id, as_of_date,
   maturity_bucket)``; ``maturity_bucket`` constrained text over the
   six canonical buckets.

All three are time-series (``as_of_date`` in the natural key),
deliberately diverging from the point-in-time
``investment_sector_weights`` (ADR-0079 §2). Each carries a NOT NULL
``basis`` discriminator (``'reported'`` | ``'computed'``).

Additively, ``investment_navs`` gains ``basis TEXT NULL`` (NULL ⇒
treated as ``'reported'`` downstream; no backfill). The existing
free-text ``source`` column is orthogonal provenance and is left
untouched.

All three new tables are tenant-scoped (per ADR-0035): ``tenant_id``
is required on every row, the standard ``apply_tenant_rls(...)``
policy is applied, and the audit trigger from b001 captures every
write. ``tenant_id`` is denormalised from ``investments`` rather than
relying on a JOIN — ADR-0035 §3 mandates row-local RLS evaluation.

The migration is fully reversible: ``downgrade`` drops triggers,
policies, indexes, and tables in dependency order, then removes the
``investment_navs.basis`` column and its CHECK, so a rollback to b015
leaves no leftover schema artefacts.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b016_add_liquid_archetypes"
down_revision: str | None = "b015_add_user_display_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- investment_bond_analytics ----------------------------------------
    op.create_table(
        "investment_bond_analytics",
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
            "investment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        # No value/sign CHECK: negative yields and spreads are valid.
        sa.Column("ytm", sa.Numeric(9, 6), nullable=False),
        sa.Column("eff_duration", sa.Numeric(6, 3), nullable=False),
        sa.Column("oas", sa.Numeric(9, 6), nullable=True),
        sa.Column("convexity", sa.Numeric(8, 3), nullable=True),
        sa.Column("basis", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "basis IN ('reported', 'computed')",
            name="ck_investment_bond_analytics_basis",
        ),
        sa.UniqueConstraint(
            "investment_id",
            "as_of_date",
            name="uq_investment_bond_analytics_investment_date",
        ),
    )
    op.create_index(
        "ix_investment_bond_analytics_tenant_investment",
        "investment_bond_analytics",
        ["tenant_id", "investment_id"],
    )

    op.execute("SELECT apply_tenant_rls('investment_bond_analytics');")

    op.execute(
        """
        CREATE TRIGGER investment_bond_analytics_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON investment_bond_analytics
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- investment_rating_weight -----------------------------------------
    op.create_table(
        "investment_rating_weight",
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
            "investment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("rating_bucket", sa.Text(), nullable=False),
        sa.Column("weight_pct", sa.Numeric(7, 4), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "rating_bucket IN ('AAA', 'AA', 'A', 'BBB', 'BB', 'B', 'CCC_and_below', 'NR')",
            name="ck_investment_rating_weight_rating_bucket",
        ),
        sa.CheckConstraint(
            "weight_pct >= 0 AND weight_pct <= 100",
            name="ck_investment_rating_weight_weight_pct_range",
        ),
        sa.CheckConstraint(
            "basis IN ('reported', 'computed')",
            name="ck_investment_rating_weight_basis",
        ),
        sa.UniqueConstraint(
            "investment_id",
            "as_of_date",
            "rating_bucket",
            name="uq_investment_rating_weight_investment_date_bucket",
        ),
    )
    op.create_index(
        "ix_investment_rating_weight_tenant_investment",
        "investment_rating_weight",
        ["tenant_id", "investment_id"],
    )

    op.execute("SELECT apply_tenant_rls('investment_rating_weight');")

    op.execute(
        """
        CREATE TRIGGER investment_rating_weight_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON investment_rating_weight
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- investment_maturity_weight ---------------------------------------
    op.create_table(
        "investment_maturity_weight",
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
            "investment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("maturity_bucket", sa.Text(), nullable=False),
        sa.Column("weight_pct", sa.Numeric(7, 4), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "maturity_bucket IN ('0-1y', '1-3y', '3-5y', '5-7y', '7-10y', '10y+')",
            name="ck_investment_maturity_weight_maturity_bucket",
        ),
        sa.CheckConstraint(
            "weight_pct >= 0 AND weight_pct <= 100",
            name="ck_investment_maturity_weight_weight_pct_range",
        ),
        sa.CheckConstraint(
            "basis IN ('reported', 'computed')",
            name="ck_investment_maturity_weight_basis",
        ),
        sa.UniqueConstraint(
            "investment_id",
            "as_of_date",
            "maturity_bucket",
            name="uq_investment_maturity_weight_investment_date_bucket",
        ),
    )
    op.create_index(
        "ix_investment_maturity_weight_tenant_investment",
        "investment_maturity_weight",
        ["tenant_id", "investment_id"],
    )

    op.execute("SELECT apply_tenant_rls('investment_maturity_weight');")

    op.execute(
        """
        CREATE TRIGGER investment_maturity_weight_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON investment_maturity_weight
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- investment_navs.basis (additive) ---------------------------------
    # Nullable with no backfill: a NULL basis is treated as 'reported'
    # by downstream code (ADR-0079 §2). The existing free-text source
    # column is orthogonal provenance and is left untouched.
    op.add_column(
        "investment_navs",
        sa.Column("basis", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_investment_navs_basis",
        "investment_navs",
        "basis IS NULL OR basis IN ('reported', 'computed')",
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Reverse order: remove the additive nav column first, then drop the
    # three new tables (each is a leaf referencing investments via FK
    # CASCADE). PostgreSQL drops policies, triggers, and indexes
    # automatically when a table is dropped, but we mirror b006/b007 and
    # drop the RLS policy + flags explicitly for symmetry.
    op.drop_constraint(
        "ck_investment_navs_basis",
        "investment_navs",
        type_="check",
    )
    op.drop_column("investment_navs", "basis")

    op.execute(
        "DROP TRIGGER IF EXISTS investment_maturity_weight_audit_trigger "
        "ON investment_maturity_weight;"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON investment_maturity_weight;")
    op.execute("ALTER TABLE investment_maturity_weight NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE investment_maturity_weight DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_investment_maturity_weight_tenant_investment",
        table_name="investment_maturity_weight",
    )
    op.drop_table("investment_maturity_weight")

    op.execute(
        "DROP TRIGGER IF EXISTS investment_rating_weight_audit_trigger ON investment_rating_weight;"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON investment_rating_weight;")
    op.execute("ALTER TABLE investment_rating_weight NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE investment_rating_weight DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_investment_rating_weight_tenant_investment",
        table_name="investment_rating_weight",
    )
    op.drop_table("investment_rating_weight")

    op.execute(
        "DROP TRIGGER IF EXISTS investment_bond_analytics_audit_trigger "
        "ON investment_bond_analytics;"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON investment_bond_analytics;")
    op.execute("ALTER TABLE investment_bond_analytics NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE investment_bond_analytics DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_investment_bond_analytics_tenant_investment",
        table_name="investment_bond_analytics",
    )
    op.drop_table("investment_bond_analytics")
