# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add countries, sectors, and per-investment country/sector weights.

Revision ID: b007_add_sector_country_weights
Revises: b006_add_investment_domain
Create Date: 2026-05-07 09:00:00 UTC

This is the Phase-5 sub-stream 5a domain migration. It introduces
the four tables that back the country and sector allocation surface
per ADR-0045 §2:

- ``countries`` — **global** ISO 3166-1 alpha-2 stammtabelle. Loaded
  from the JSON fixture under
  ``services/data_normalization/fixtures/iso_3166_1_alpha_2.json``.
  Includes the reserved sentinel ``XX`` for unallocated splits. The
  table is **not** RLS-protected: every tenant reads the same set of
  countries. The ``test_rls_schema_invariants`` regression guard
  carries an allow-list entry that documents this exception.
- ``sectors`` — **tenant-scoped** catalogue of sector definitions.
  Each tenant curates its own sector vocabulary. Per-tenant
  ``unclassified`` rows are installed by ``portfoliflow bootstrap``
  (not by this migration), mirroring the asset-class pattern from
  ADR-0043 §1.
- ``investment_country_weights`` — tenant-scoped per-investment
  allocation across countries. Identified by ``(investment_id,
  country_iso_code)``. ``weight_pct`` is a percentage in the closed
  interval ``[0, 100]``.
- ``investment_sector_weights`` — tenant-scoped per-investment
  allocation across sectors. Identified by ``(investment_id,
  sector_id)``. ``weight_pct`` follows the same convention.

The three tenant-scoped tables apply ``apply_tenant_rls(...)``,
denormalise ``tenant_id`` for row-local RLS evaluation per
ADR-0035 §3, and carry the standard audit trigger from b001.

The migration is fully reversible: ``downgrade`` drops triggers,
policies, indexes, and tables in dependency order so a rollback to
b006 leaves no leftover schema artefacts. The country fixture seed is
installed via ``INSERT ... ON CONFLICT DO NOTHING`` so re-running the
upgrade after an aborted run is idempotent.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "b007_add_sector_country_weights"
down_revision: str | None = "b006_add_investment_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Locate the ISO fixture relative to the repo root. The migration file
# lives at db/migrations/versions/<file>.py; the fixture lives at
# services/data_normalization/fixtures/<file>.json.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE_PATH = (
    _REPO_ROOT / "services" / "data_normalization" / "fixtures" / "iso_3166_1_alpha_2.json"
)


def _load_country_fixture() -> list[dict[str, str]]:
    """Read the ISO 3166-1 alpha-2 fixture used to seed ``countries``.

    Raises a hard error if the fixture is missing — that is a packaging
    fault and must surface loudly rather than produce an empty seed.
    """
    if not _FIXTURE_PATH.exists():
        raise FileNotFoundError(
            f"Country fixture not found at {_FIXTURE_PATH!s}. The b007 "
            "migration requires the JSON seed file shipped under "
            "services/data_normalization/fixtures/."
        )
    with _FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Country fixture at {_FIXTURE_PATH!s} is empty or malformed.")
    return payload


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- countries (global, NOT RLS-protected) -----------------------------
    op.create_table(
        "countries",
        sa.Column("iso_code", sa.CHAR(2), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("region_default", sa.Text(), nullable=False),
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
    )
    # No apply_tenant_rls() and no audit trigger by design — countries
    # is a global lookup table. The companion regression guard in
    # tests/regression/test_rls_schema_invariants.py carries an
    # allow-list entry documenting the exception.

    # Seed via ON CONFLICT DO NOTHING so the upgrade is idempotent even
    # if a partial run already inserted some rows.
    fixture = _load_country_fixture()
    countries_table = sa.table(
        "countries",
        sa.column("iso_code", sa.CHAR(2)),
        sa.column("display_name", sa.Text()),
        sa.column("region_default", sa.Text()),
    )
    insert_stmt = sa.dialects.postgresql.insert(countries_table).values(
        [
            {
                "iso_code": entry["iso_code"],
                "display_name": entry["display_name"],
                "region_default": entry["region_default"],
            }
            for entry in fixture
        ]
    )
    op.execute(insert_stmt.on_conflict_do_nothing(index_elements=["iso_code"]))

    # ---- sectors (tenant-scoped catalogue) ---------------------------------
    op.create_table(
        "sectors",
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
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
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
        sa.UniqueConstraint("tenant_id", "code", name="uq_sectors_tenant_code"),
    )
    op.create_index("ix_sectors_tenant_id", "sectors", ["tenant_id"])

    op.execute("SELECT apply_tenant_rls('sectors');")

    op.execute(
        """
        CREATE TRIGGER sectors_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON sectors
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- investment_country_weights ---------------------------------------
    op.create_table(
        "investment_country_weights",
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
            "country_iso_code",
            sa.CHAR(2),
            sa.ForeignKey("countries.iso_code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("weight_pct", sa.Numeric(7, 4), nullable=False),
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
            "weight_pct >= 0 AND weight_pct <= 100",
            name="ck_investment_country_weights_weight_pct_range",
        ),
        sa.UniqueConstraint(
            "investment_id",
            "country_iso_code",
            name="uq_investment_country_weights_investment_country",
        ),
    )
    op.create_index(
        "ix_investment_country_weights_tenant_investment",
        "investment_country_weights",
        ["tenant_id", "investment_id"],
    )
    op.create_index(
        "ix_investment_country_weights_tenant_country",
        "investment_country_weights",
        ["tenant_id", "country_iso_code"],
    )

    op.execute("SELECT apply_tenant_rls('investment_country_weights');")

    op.execute(
        """
        CREATE TRIGGER investment_country_weights_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON investment_country_weights
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- investment_sector_weights ----------------------------------------
    op.create_table(
        "investment_sector_weights",
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
            "sector_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sectors.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("weight_pct", sa.Numeric(7, 4), nullable=False),
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
            "weight_pct >= 0 AND weight_pct <= 100",
            name="ck_investment_sector_weights_weight_pct_range",
        ),
        sa.UniqueConstraint(
            "investment_id",
            "sector_id",
            name="uq_investment_sector_weights_investment_sector",
        ),
    )
    op.create_index(
        "ix_investment_sector_weights_tenant_investment",
        "investment_sector_weights",
        ["tenant_id", "investment_id"],
    )
    op.create_index(
        "ix_investment_sector_weights_tenant_sector",
        "investment_sector_weights",
        ["tenant_id", "sector_id"],
    )

    op.execute("SELECT apply_tenant_rls('investment_sector_weights');")

    op.execute(
        """
        CREATE TRIGGER investment_sector_weights_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON investment_sector_weights
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Reverse-dependency order: weights tables reference investments,
    # countries, sectors; sectors references tenants and users; countries
    # is the leaf among the four (it has no FKs).
    op.execute(
        "DROP TRIGGER IF EXISTS investment_sector_weights_audit_trigger "
        "ON investment_sector_weights;"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON investment_sector_weights;")
    op.execute("ALTER TABLE investment_sector_weights NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE investment_sector_weights DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_investment_sector_weights_tenant_sector",
        table_name="investment_sector_weights",
    )
    op.drop_index(
        "ix_investment_sector_weights_tenant_investment",
        table_name="investment_sector_weights",
    )
    op.drop_table("investment_sector_weights")

    op.execute(
        "DROP TRIGGER IF EXISTS investment_country_weights_audit_trigger "
        "ON investment_country_weights;"
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON investment_country_weights;")
    op.execute("ALTER TABLE investment_country_weights NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE investment_country_weights DISABLE ROW LEVEL SECURITY;")
    op.drop_index(
        "ix_investment_country_weights_tenant_country",
        table_name="investment_country_weights",
    )
    op.drop_index(
        "ix_investment_country_weights_tenant_investment",
        table_name="investment_country_weights",
    )
    op.drop_table("investment_country_weights")

    op.execute("DROP TRIGGER IF EXISTS sectors_audit_trigger ON sectors;")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON sectors;")
    op.execute("ALTER TABLE sectors NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE sectors DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_sectors_tenant_id", table_name="sectors")
    op.drop_table("sectors")

    op.drop_table("countries")
