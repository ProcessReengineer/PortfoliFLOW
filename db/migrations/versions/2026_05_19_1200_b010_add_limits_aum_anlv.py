# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add anlv_categories, investments.anlv_code, portfolio_aum, limit_sets, limits.

Revision ID: b010_add_limits_aum_anlv
Revises: b009_add_regions_table
Create Date: 2026-05-19 12:00:00 UTC

The Phase-7 Anlagegrenzen-Überwachung data layer per ADR-0055,
ADR-0056, and ADR-0057. Five concerns land together in a single
migration because they share one feature scope and the project is
still in active development against test data only (db-reset
workflow — no production migrations need reversibility):

1. ``anlv_categories`` — **global** stammtabelle (no tenant_id, no
   RLS, no audit trigger), same pattern as ``countries``. Seeded
   from ``services/data_normalization/fixtures/anlv_categories.json``
   via ``INSERT ... ON CONFLICT DO NOTHING``. ADR-0057.
2. ``investments.anlv_code`` — nullable text column with FK to
   ``anlv_categories.code``. Partial index on the column excludes
   NULLs (the "AnlV unallocated" engine bucket is a NULL state, not
   a query target). ADR-0057.
3. ``portfolio_aum`` — tenant-scoped daily AUM time-series. Cash is
   the residual ``aum_eur − Σ NAVs`` at evaluation time, not a
   persisted entity. ADR-0055.
4. ``limit_sets`` — tenant-scoped (family, effective_from) catalogue
   of limit sets. ``family IN ('saa', 'anlv')`` per ADR-0056 (the
   ``'saa'`` discriminator replaces the placeholder ``'satzung'``
   from earlier drafts).
5. ``limits`` — tenant-scoped (limit_set_id, class_key) row table.
   ``tenant_id`` is denormalised from ``limit_sets.tenant_id`` for
   row-local RLS evaluation per ADR-0035 §3. ``class_key`` is a
   string snapshot, not an FK — resolution against
   ``asset_classes.code`` (saa) or ``anlv_categories.code`` (anlv)
   is performed family-polymorphically in the importer.

The migration does **not** install the ``unclassified`` asset class
or the 12 default asset classes — those are bootstrap-time
operations (``cli/bootstrap.py``), not schema concerns.

``downgrade()`` is intentionally minimal: ``pass``. The project's
db-reset workflow re-applies the chain from scratch rather than
rolling back. Production-grade reversibility is added if and when
the project moves to a managed environment.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "b010_add_limits_aum_anlv"
down_revision: str | None = "b009_add_regions_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REPO_ROOT = Path(__file__).resolve().parents[3]
_ANLV_FIXTURE_PATH = (
    _REPO_ROOT / "services" / "data_normalization" / "fixtures" / "anlv_categories.json"
)


def _load_anlv_fixture() -> list[dict[str, object]]:
    """Read the AnlV categories fixture used to seed ``anlv_categories``.

    Same hard-error policy as the b007 country fixture loader: a
    missing fixture is a packaging fault and must surface loudly.
    """
    if not _ANLV_FIXTURE_PATH.exists():
        raise FileNotFoundError(
            f"AnlV fixture not found at {_ANLV_FIXTURE_PATH!s}. The b010 "
            "migration requires the JSON seed file shipped under "
            "services/data_normalization/fixtures/."
        )
    with _ANLV_FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"AnlV fixture at {_ANLV_FIXTURE_PATH!s} is empty or malformed.")
    return payload


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- anlv_categories (global, NOT RLS-protected) ----------------------
    op.create_table(
        "anlv_categories",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("paragraph_label", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
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
    # No apply_tenant_rls() and no audit trigger — global lookup. The
    # tests/regression/test_rls_schema_invariants regression guard
    # carries an allow-list entry that documents this exception.

    fixture = _load_anlv_fixture()
    anlv_table = sa.table(
        "anlv_categories",
        sa.column("code", sa.Text()),
        sa.column("paragraph_label", sa.Text()),
        sa.column("display_name", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("sort_order", sa.Integer()),
    )
    insert_stmt = sa.dialects.postgresql.insert(anlv_table).values(
        [
            {
                "code": entry["code"],
                "paragraph_label": entry["paragraph_label"],
                "display_name": entry["display_name"],
                "description": entry.get("description"),
                "sort_order": entry["sort_order"],
            }
            for entry in fixture
        ]
    )
    op.execute(insert_stmt.on_conflict_do_nothing(index_elements=["code"]))

    # ---- investments.anlv_code (FK + partial index) -----------------------
    op.add_column(
        "investments",
        sa.Column(
            "anlv_code",
            sa.Text(),
            sa.ForeignKey(
                "anlv_categories.code",
                name="fk_investments_anlv_code",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
    )
    op.execute(
        "CREATE INDEX ix_investments_anlv_code "
        "ON investments(anlv_code) "
        "WHERE anlv_code IS NOT NULL"
    )

    # ---- portfolio_aum (tenant-scoped) ------------------------------------
    op.create_table(
        "portfolio_aum",
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
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("aum_eur", sa.Numeric(20, 4), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
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
        sa.CheckConstraint("aum_eur > 0", name="ck_portfolio_aum_positive"),
        sa.UniqueConstraint("tenant_id", "as_of_date", name="uq_portfolio_aum_tenant_date"),
    )
    op.create_index(
        "ix_portfolio_aum_tenant_date",
        "portfolio_aum",
        ["tenant_id", sa.text("as_of_date DESC")],
    )
    op.execute("SELECT apply_tenant_rls('portfolio_aum');")
    op.execute(
        """
        CREATE TRIGGER portfolio_aum_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON portfolio_aum
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- limit_sets (tenant-scoped, family-discriminated) -----------------
    op.create_table(
        "limit_sets",
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
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
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
            "family IN ('saa', 'anlv')",
            name="ck_limit_sets_family",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "family",
            "effective_from",
            name="uq_limit_sets_tenant_family_effective_from",
        ),
    )
    op.create_index(
        "ix_limit_sets_tenant_family_effective_from",
        "limit_sets",
        ["tenant_id", "family", sa.text("effective_from DESC")],
    )
    op.execute("SELECT apply_tenant_rls('limit_sets');")
    op.execute(
        """
        CREATE TRIGGER limit_sets_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON limit_sets
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- limits (tenant-scoped, denormalised tenant_id for RLS) -----------
    op.create_table(
        "limits",
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
            "limit_set_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("limit_sets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("class_key", sa.Text(), nullable=False),
        sa.Column("max_pct", sa.Numeric(7, 4), nullable=False),
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
            "max_pct > 0 AND max_pct <= 100",
            name="ck_limits_max_pct_range",
        ),
        sa.UniqueConstraint(
            "limit_set_id",
            "class_key",
            name="uq_limits_set_class_key",
        ),
    )
    op.create_index("ix_limits_tenant_set", "limits", ["tenant_id", "limit_set_id"])
    op.execute("SELECT apply_tenant_rls('limits');")
    op.execute(
        """
        CREATE TRIGGER limits_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON limits
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # The project's db-reset workflow re-applies the chain from
    # scratch rather than rolling back. Production-grade reversibility
    # can be added when the project moves to a managed environment.
    pass
