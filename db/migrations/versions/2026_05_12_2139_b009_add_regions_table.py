# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add regions, region_country_memberships, and investment_region_weights.

Revision ID: b009_add_regions_table
Revises: b008_seed_default_tenant
Create Date: 2026-05-12 21:39:00 UTC

Phase-6 region-model sub-stream — ADR-0046. Replaces the
closure-debug Stufe-A country auto-create technical debt with a
purpose-built region aggregation layer:

- ``regions`` — per-tenant catalogue of region definitions
  (``"DACH"``, ``"Asia Emerging"``, …). Pre-seeded by
  ``portfoliflow bootstrap``; not Excel-driven.
- ``region_country_memberships`` — many-to-one mapping between
  regions and ISO-3166-1-alpha-2 countries. The UNIQUE constraint on
  ``(tenant_id, country_iso_code)`` enforces M1 strict-partition:
  every country belongs to at most one region per tenant.
- ``investment_region_weights`` — per-investment region allocations
  populated by the Excel import path. Replaces the Excel-pathway
  use of ``investment_country_weights``; the latter table stays in
  the schema but is now reserved for ISO-granular data sources
  (roadmap A2/A3 — GP report scrapers).

The migration starts with two cleanup steps that drop the Stufe-A
debris from the closure-debug auto-create path:

1. Delete every ``investment_country_weights`` row whose
   ``country_iso_code`` is not a real two-letter ISO code.
2. Delete every ``countries`` row whose ``iso_code`` is not a real
   two-letter uppercase ISO code (the Stufe-A auto-created strings
   like ``"dach"``, ``"north_america_usa"``, …).

These cleanup steps are intentionally not reversible — the ``downgrade``
path can drop the new tables but cannot reconstruct the deleted
junk rows. That is documented behaviour: the debt-tilgung is a
one-way operation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b009_add_regions_table"
down_revision: str | None = "b008_seed_default_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- Cleanup step 1: drop weight rows pointing at non-ISO codes ---------
    # The closure-debug Stufe-A auto-create path persisted Excel region
    # labels (``"dach"``, …) as ``country_iso_code`` values. They are not
    # valid ISO codes and have to be cleared before we re-enforce the FK
    # invariant via the new code path.
    op.execute(
        """
        DELETE FROM investment_country_weights
        WHERE country_iso_code NOT IN (
            SELECT iso_code FROM countries
            WHERE LENGTH(iso_code) = 2 AND iso_code = UPPER(iso_code)
        )
        """
    )

    # ---- Cleanup step 2: drop junk rows from the countries stammtabelle ----
    # The b007 seed loads real ISO codes; the closure-debug auto-create
    # path appended additional non-ISO rows like ``"dach"``. Remove them
    # so ``countries`` is exclusively the ISO-3166-1-alpha-2 stammtabelle
    # again, with the ``XX`` sentinel as the only documented exception.
    op.execute(
        """
        DELETE FROM countries
        WHERE NOT (LENGTH(iso_code) = 2 AND iso_code = UPPER(iso_code))
        """
    )

    # ---- Backfill ISO codes the default region catalogue depends on --------
    # The original b007 fixture predates the Phase-6 region catalogue and
    # does not carry every code the default memberships need (most
    # notably ``XK``, the SWIFT/IATA/EU user-assigned code for Kosovo,
    # which is reserved in ISO 3166-1 but not assigned). The b007
    # fixture itself is updated in lockstep so a fresh ``alembic
    # upgrade`` from scratch carries the row; this INSERT covers
    # already-migrated DBs.
    op.execute(
        """
        INSERT INTO countries (iso_code, display_name, region_default)
        VALUES ('XK', 'Kosovo', 'Eastern Europe')
        ON CONFLICT (iso_code) DO NOTHING
        """
    )

    # ---- regions (per-tenant catalogue) ------------------------------------
    op.create_table(
        "regions",
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
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
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
        sa.UniqueConstraint("tenant_id", "code", name="uq_regions_tenant_code"),
    )
    op.create_index("ix_regions_tenant_id", "regions", ["tenant_id"])

    op.execute("SELECT apply_tenant_rls('regions');")

    op.execute(
        """
        CREATE TRIGGER regions_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON regions
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- region_country_memberships ----------------------------------------
    op.create_table(
        "region_country_memberships",
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
            "region_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("regions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "country_iso_code",
            sa.CHAR(2),
            sa.ForeignKey("countries.iso_code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "country_iso_code",
            name="uq_region_country_memberships_tenant_iso_unique",
        ),
    )
    op.create_index(
        "ix_region_country_memberships_tenant_id",
        "region_country_memberships",
        ["tenant_id"],
    )
    op.create_index(
        "ix_region_country_memberships_region_id",
        "region_country_memberships",
        ["region_id"],
    )

    op.execute("SELECT apply_tenant_rls('region_country_memberships');")

    op.execute(
        """
        CREATE TRIGGER region_country_memberships_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON region_country_memberships
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- investment_region_weights -----------------------------------------
    op.create_table(
        "investment_region_weights",
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
            "region_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("regions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("weight_pct", sa.Numeric(8, 4), nullable=False),
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
        sa.CheckConstraint("weight_pct >= 0", name="ck_region_weight_non_negative"),
        sa.CheckConstraint("weight_pct <= 100", name="ck_region_weight_max"),
        sa.UniqueConstraint(
            "tenant_id",
            "investment_id",
            "region_id",
            name="uq_investment_region_weights_inv_region_unique",
        ),
    )
    op.create_index(
        "ix_investment_region_weights_tenant_investment",
        "investment_region_weights",
        ["tenant_id", "investment_id"],
    )
    op.create_index(
        "ix_investment_region_weights_tenant_region",
        "investment_region_weights",
        ["tenant_id", "region_id"],
    )

    op.execute("SELECT apply_tenant_rls('investment_region_weights');")

    op.execute(
        """
        CREATE TRIGGER investment_region_weights_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON investment_region_weights
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- Document the legacy country-weights table's reduced scope --------
    op.execute(
        """
        COMMENT ON TABLE investment_country_weights IS
        'ISO-3166-1-alpha-2 country weights per investment. Reserved '
        'for GP-report ingestion (roadmap A2/A3) and other sources that '
        'deliver ISO-level granularity. The Excel import path writes '
        'investment_region_weights instead; see ADR-0046 (regions '
        'model).'
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # The two cleanup steps in ``upgrade()`` are deliberately *not*
    # reversed here: the deleted rows were debris from the closure-debug
    # auto-create path and there is no way to reconstruct them. The
    # ``investment_country_weights`` table comment is reverted to NULL.

    op.execute("COMMENT ON TABLE investment_country_weights IS NULL")

    op.execute(
        "DROP TRIGGER IF EXISTS investment_region_weights_audit_trigger "
        "ON investment_region_weights;"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON investment_region_weights;")
    op.execute("ALTER TABLE investment_region_weights NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE investment_region_weights DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_investment_region_weights_tenant_region",
        table_name="investment_region_weights",
    )
    op.drop_index(
        "ix_investment_region_weights_tenant_investment",
        table_name="investment_region_weights",
    )
    op.drop_table("investment_region_weights")

    op.execute(
        "DROP TRIGGER IF EXISTS region_country_memberships_audit_trigger "
        "ON region_country_memberships;"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON region_country_memberships;")
    op.execute("ALTER TABLE region_country_memberships NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE region_country_memberships DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_region_country_memberships_region_id",
        table_name="region_country_memberships",
    )
    op.drop_index(
        "ix_region_country_memberships_tenant_id",
        table_name="region_country_memberships",
    )
    op.drop_table("region_country_memberships")

    op.execute("DROP TRIGGER IF EXISTS regions_audit_trigger ON regions;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON regions;")
    op.execute("ALTER TABLE regions NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE regions DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_regions_tenant_id", table_name="regions")
    op.drop_table("regions")
