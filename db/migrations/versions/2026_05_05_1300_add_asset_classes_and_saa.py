# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add asset_classes and SAA configuration tables.

Revision ID: b005_add_asset_classes_and_saa
Revises: b004_add_data_uploads_tables
Create Date: 2026-05-05 13:00:00 UTC

This is the sub-stream 3b domain migration. It introduces the four
tenant-scoped tables that back the web Strategic Asset Allocation
workflow per ADR-0042 §1:

- ``asset_classes`` — per-tenant catalogue of asset-class definitions.
- ``saa_configurations`` — top-level SAA configuration entity. The
  partial unique index ``uq_saa_configurations_active_per_tenant``
  enforces "at most one active configuration per tenant" without
  serialising every write through a transaction-level lock.
- ``saa_asset_class_inputs`` — per-configuration, per-asset-class
  expectations and weight bounds.
- ``saa_correlations`` — upper-triangle correlation triplets keyed on
  ``(configuration_id, asset_class_a_id, asset_class_b_id)``. The
  ``asset_class_a_id < asset_class_b_id`` CHECK enforces upper-
  triangle storage by UUID order. The diagonal (always 1.0) and the
  lower triangle are not stored — the service layer fills them in when
  constructing the correlation matrix for the optimiser.

All four tables are tenant-scoped (per ADR-0035): ``tenant_id`` is
required on every row, the standard ``apply_tenant_rls(...)`` policy
is applied, and the audit trigger from b001 captures every write.
``saa_asset_class_inputs`` and ``saa_correlations`` denormalise
``tenant_id`` rather than relying on a JOIN against the parent
``saa_configurations`` row — ADR-0035 §3 mandates row-local RLS
evaluation without joins.

The migration is fully reversible: ``downgrade`` drops triggers,
policies, indexes, and tables in dependency order so a rollback to
b004 leaves no leftover schema artefacts.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b005_add_asset_classes_and_saa"
down_revision: str | None = "b004_add_data_uploads_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- asset_classes -----------------------------------------------------
    op.create_table(
        "asset_classes",
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
        sa.UniqueConstraint("tenant_id", "code", name="uq_asset_classes_tenant_code"),
    )
    op.create_index("ix_asset_classes_tenant_id", "asset_classes", ["tenant_id"])

    op.execute("SELECT apply_tenant_rls('asset_classes');")

    op.execute(
        """
        CREATE TRIGGER asset_classes_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON asset_classes
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- saa_configurations ------------------------------------------------
    op.create_table(
        "saa_configurations",
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
        sa.Column(
            "risk_free_rate",
            sa.Numeric(8, 6),
            nullable=False,
            server_default=sa.text("0.0"),
        ),
        sa.Column(
            "n_frontier_points",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("100"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
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
            "n_frontier_points >= 20 AND n_frontier_points <= 500",
            name="ck_saa_configurations_n_frontier_points_range",
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_saa_configurations_tenant_name"),
    )
    op.create_index(
        "ix_saa_configurations_tenant_id",
        "saa_configurations",
        ["tenant_id"],
    )
    # Partial unique index: at most one active configuration per tenant.
    # The standard UNIQUE constraint cannot express the ``WHERE``
    # predicate, so a ``CREATE UNIQUE INDEX`` is the right tool.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_saa_configurations_active_per_tenant
        ON saa_configurations (tenant_id) WHERE is_active = TRUE;
        """
    )

    op.execute("SELECT apply_tenant_rls('saa_configurations');")

    op.execute(
        """
        CREATE TRIGGER saa_configurations_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON saa_configurations
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- saa_asset_class_inputs --------------------------------------------
    op.create_table(
        "saa_asset_class_inputs",
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
            "configuration_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("saa_configurations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_class_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("asset_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("expected_return", sa.Numeric(8, 6), nullable=False),
        sa.Column("volatility", sa.Numeric(8, 6), nullable=False),
        sa.Column(
            "min_weight",
            sa.Numeric(8, 6),
            nullable=False,
            server_default=sa.text("0.0"),
        ),
        sa.Column(
            "max_weight",
            sa.Numeric(8, 6),
            nullable=False,
            server_default=sa.text("1.0"),
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
            "volatility >= 0",
            name="ck_saa_asset_class_inputs_volatility_nonneg",
        ),
        sa.CheckConstraint(
            "min_weight >= 0 AND min_weight <= 1",
            name="ck_saa_asset_class_inputs_min_weight_range",
        ),
        sa.CheckConstraint(
            "max_weight >= 0 AND max_weight <= 1",
            name="ck_saa_asset_class_inputs_max_weight_range",
        ),
        sa.CheckConstraint(
            "min_weight <= max_weight",
            name="ck_saa_asset_class_inputs_min_le_max",
        ),
        sa.UniqueConstraint(
            "configuration_id",
            "asset_class_id",
            name="uq_saa_asset_class_inputs_config_asset",
        ),
    )
    op.create_index(
        "ix_saa_asset_class_inputs_tenant_id",
        "saa_asset_class_inputs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_saa_asset_class_inputs_configuration_id",
        "saa_asset_class_inputs",
        ["configuration_id"],
    )

    op.execute("SELECT apply_tenant_rls('saa_asset_class_inputs');")

    op.execute(
        """
        CREATE TRIGGER saa_asset_class_inputs_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON saa_asset_class_inputs
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- saa_correlations --------------------------------------------------
    op.create_table(
        "saa_correlations",
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
            "configuration_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("saa_configurations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_class_a_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("asset_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "asset_class_b_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("asset_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("correlation", sa.Numeric(8, 6), nullable=False),
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
            "correlation >= -1 AND correlation <= 1",
            name="ck_saa_correlations_correlation_range",
        ),
        # Upper-triangle storage: ordering by UUID guarantees each
        # asset-class pair appears at most once. The service layer
        # normalises caller-supplied pairs before persisting; this
        # CHECK is the database-level guard that catches a bypass.
        sa.CheckConstraint(
            "asset_class_a_id < asset_class_b_id",
            name="ck_saa_correlations_upper_triangle",
        ),
        sa.UniqueConstraint(
            "configuration_id",
            "asset_class_a_id",
            "asset_class_b_id",
            name="uq_saa_correlations_config_pair",
        ),
    )
    op.create_index(
        "ix_saa_correlations_tenant_id",
        "saa_correlations",
        ["tenant_id"],
    )
    op.create_index(
        "ix_saa_correlations_configuration_id",
        "saa_correlations",
        ["configuration_id"],
    )

    op.execute("SELECT apply_tenant_rls('saa_correlations');")

    op.execute(
        """
        CREATE TRIGGER saa_correlations_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON saa_correlations
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Drop in reverse dependency order: correlations and inputs both
    # reference configurations and asset_classes; configurations
    # references users; asset_classes is the leaf among the four.
    op.execute("DROP TRIGGER IF EXISTS saa_correlations_audit_trigger ON saa_correlations;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON saa_correlations;")
    op.execute("ALTER TABLE saa_correlations NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE saa_correlations DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_saa_correlations_configuration_id", table_name="saa_correlations")
    op.drop_index("ix_saa_correlations_tenant_id", table_name="saa_correlations")
    op.drop_table("saa_correlations")

    op.execute(
        "DROP TRIGGER IF EXISTS saa_asset_class_inputs_audit_trigger ON saa_asset_class_inputs;"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON saa_asset_class_inputs;")
    op.execute("ALTER TABLE saa_asset_class_inputs NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE saa_asset_class_inputs DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_saa_asset_class_inputs_configuration_id",
        table_name="saa_asset_class_inputs",
    )
    op.drop_index(
        "ix_saa_asset_class_inputs_tenant_id",
        table_name="saa_asset_class_inputs",
    )
    op.drop_table("saa_asset_class_inputs")

    op.execute("DROP TRIGGER IF EXISTS saa_configurations_audit_trigger ON saa_configurations;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON saa_configurations;")
    op.execute("ALTER TABLE saa_configurations NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE saa_configurations DISABLE ROW LEVEL SECURITY;")
    op.execute("DROP INDEX IF EXISTS uq_saa_configurations_active_per_tenant;")
    op.drop_index("ix_saa_configurations_tenant_id", table_name="saa_configurations")
    op.drop_table("saa_configurations")

    op.execute("DROP TRIGGER IF EXISTS asset_classes_audit_trigger ON asset_classes;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON asset_classes;")
    op.execute("ALTER TABLE asset_classes NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE asset_classes DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_asset_classes_tenant_id", table_name="asset_classes")
    op.drop_table("asset_classes")
