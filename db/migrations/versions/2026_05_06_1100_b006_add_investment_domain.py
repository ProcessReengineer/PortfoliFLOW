# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add investments, investment_navs, and investment_cashflows tables.

Revision ID: b006_add_investment_domain
Revises: b005_add_asset_classes_and_saa
Create Date: 2026-05-06 11:00:00 UTC

This is the sub-stream 4a domain migration. It introduces the three
tenant-scoped tables that back the web Investment domain per
ADR-0043 §1:

- ``investments`` — one row per investment instrument, with a flat
  polymorphic ``investment_type`` discriminator (seven allowed
  values: ``private_equity``, ``private_debt``, ``real_estate``,
  ``infra_equity``, ``listed_equity``, ``listed_bonds``, ``other``).
  All seven types share the same column structure in Phase 4. A
  ``type_specific_data`` JSONB column is reserved as an emergency
  exit for Phase-5+ extensions but is not used in Phase 4.
- ``investment_navs`` — date-stamped valuations per investment with
  a ``nav_kind`` discriminator (``plan`` | ``actual``). Plan and
  actual series are stored in parallel; neither is overwritten when
  the other changes. ``as_of_date`` is a ``DATE`` (statement-day
  semantics, not point-in-time).
- ``investment_cashflows`` — cashflow events per investment with a
  ``flow_type`` discriminator (seven values: ``capital_call``,
  ``distribution``, ``fee``, ``carry``, ``dividend``, ``coupon``,
  ``other``) and a ``flow_kind`` discriminator (``plan`` | ``actual``).
  ``flow_timestamp`` is a ``TIMESTAMPTZ`` with a default convention
  of 12:00 UTC when the precise time is unknown. There is no UNIQUE
  constraint — multiple cashflows per investment / timestamp / type
  / kind are permitted.

All three tables are tenant-scoped (per ADR-0035): ``tenant_id`` is
required on every row, the standard ``apply_tenant_rls(...)`` policy
is applied, and the audit trigger from b001 captures every write.
``investment_navs`` and ``investment_cashflows`` denormalise
``tenant_id`` rather than relying on a JOIN against the parent
``investments`` row — ADR-0035 §3 mandates row-local RLS evaluation
without joins.

The migration is fully reversible: ``downgrade`` drops triggers,
policies, indexes, and tables in dependency order so a rollback to
b005 leaves no leftover schema artefacts.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b006_add_investment_domain"
down_revision: str | None = "b005_add_asset_classes_and_saa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- investments -------------------------------------------------------
    op.create_table(
        "investments",
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
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("investment_type", sa.Text(), nullable=False),
        sa.Column(
            "asset_class_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("asset_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("manager_name", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("vintage_year", sa.Integer(), nullable=True),
        sa.Column("commitment_amount", sa.Numeric(20, 4), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column(
            "type_specific_data",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
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
            "investment_type IN ("
            "'private_equity', 'private_debt', 'real_estate', "
            "'infra_equity', 'listed_equity', 'listed_bonds', 'other'"
            ")",
            name="ck_investments_investment_type",
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_investments_tenant_name"),
    )
    op.create_index("ix_investments_tenant_id", "investments", ["tenant_id"])
    op.create_index(
        "ix_investments_tenant_investment_type",
        "investments",
        ["tenant_id", "investment_type"],
    )
    op.create_index(
        "ix_investments_tenant_asset_class_id",
        "investments",
        ["tenant_id", "asset_class_id"],
    )
    op.create_index(
        "ix_investments_tenant_is_active",
        "investments",
        ["tenant_id", "is_active"],
    )

    op.execute("SELECT apply_tenant_rls('investments');")

    op.execute(
        """
        CREATE TRIGGER investments_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON investments
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- investment_navs ---------------------------------------------------
    op.create_table(
        "investment_navs",
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
        sa.Column("nav_value", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("nav_kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
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
            "nav_kind IN ('plan', 'actual')",
            name="ck_investment_navs_nav_kind",
        ),
        sa.UniqueConstraint(
            "investment_id",
            "as_of_date",
            "nav_kind",
            name="uq_investment_navs_investment_date_kind",
        ),
    )
    op.create_index(
        "ix_investment_navs_tenant_id",
        "investment_navs",
        ["tenant_id"],
    )
    # Descending index on as_of_date supports "latest valuation per
    # investment" lookups without a Sort node on top of the scan.
    op.execute(
        """
        CREATE INDEX ix_investment_navs_investment_as_of_date_desc
        ON investment_navs (investment_id, as_of_date DESC);
        """
    )
    op.create_index(
        "ix_investment_navs_tenant_as_of_date",
        "investment_navs",
        ["tenant_id", "as_of_date"],
    )

    op.execute("SELECT apply_tenant_rls('investment_navs');")

    op.execute(
        """
        CREATE TRIGGER investment_navs_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON investment_navs
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- investment_cashflows ----------------------------------------------
    op.create_table(
        "investment_cashflows",
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
        sa.Column(
            "flow_timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("flow_type", sa.Text(), nullable=False),
        sa.Column("flow_kind", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
            "flow_type IN ("
            "'capital_call', 'distribution', 'fee', 'carry', "
            "'dividend', 'coupon', 'other'"
            ")",
            name="ck_investment_cashflows_flow_type",
        ),
        sa.CheckConstraint(
            "flow_kind IN ('plan', 'actual')",
            name="ck_investment_cashflows_flow_kind",
        ),
    )
    op.create_index(
        "ix_investment_cashflows_tenant_id",
        "investment_cashflows",
        ["tenant_id"],
    )
    op.create_index(
        "ix_investment_cashflows_investment_timestamp_kind",
        "investment_cashflows",
        ["investment_id", "flow_timestamp", "flow_kind"],
    )
    op.create_index(
        "ix_investment_cashflows_tenant_timestamp",
        "investment_cashflows",
        ["tenant_id", "flow_timestamp"],
    )

    op.execute("SELECT apply_tenant_rls('investment_cashflows');")

    op.execute(
        """
        CREATE TRIGGER investment_cashflows_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON investment_cashflows
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Drop in reverse dependency order: cashflows and navs both
    # reference investments via FK CASCADE; investments references
    # asset_classes (RESTRICT) and users (RESTRICT) and is the leaf
    # among the three new tables.
    op.execute("DROP TRIGGER IF EXISTS investment_cashflows_audit_trigger ON investment_cashflows;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON investment_cashflows;")
    op.execute("ALTER TABLE investment_cashflows NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE investment_cashflows DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_investment_cashflows_tenant_timestamp",
        table_name="investment_cashflows",
    )
    op.drop_index(
        "ix_investment_cashflows_investment_timestamp_kind",
        table_name="investment_cashflows",
    )
    op.drop_index(
        "ix_investment_cashflows_tenant_id",
        table_name="investment_cashflows",
    )
    op.drop_table("investment_cashflows")

    op.execute("DROP TRIGGER IF EXISTS investment_navs_audit_trigger ON investment_navs;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON investment_navs;")
    op.execute("ALTER TABLE investment_navs NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE investment_navs DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_investment_navs_tenant_as_of_date",
        table_name="investment_navs",
    )
    op.execute("DROP INDEX IF EXISTS ix_investment_navs_investment_as_of_date_desc;")
    op.drop_index(
        "ix_investment_navs_tenant_id",
        table_name="investment_navs",
    )
    op.drop_table("investment_navs")

    op.execute("DROP TRIGGER IF EXISTS investments_audit_trigger ON investments;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON investments;")
    op.execute("ALTER TABLE investments NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE investments DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_investments_tenant_is_active", table_name="investments")
    op.drop_index("ix_investments_tenant_asset_class_id", table_name="investments")
    op.drop_index("ix_investments_tenant_investment_type", table_name="investments")
    op.drop_index("ix_investments_tenant_id", table_name="investments")
    op.drop_table("investments")
