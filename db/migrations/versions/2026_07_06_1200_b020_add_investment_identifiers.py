# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add investment_identifiers: security identifiers as the external join-key.

Revision ID: b020_add_investment_identifiers
Revises: b019_add_irene_persistence
Create Date: 2026-07-06 12:00:00 UTC

The first implementation slice of Live Data Import (roadmap #036), per
ADR-0090. Introduces one tenant-scoped table, ``investment_identifiers``,
holding the security identifiers (ISIN / ticker / FIGI / CUSIP / internal)
that make an existing investment addressable against external market-data
providers. Identifiers live in a dedicated child table rather than as
columns on ``investments`` because a private-markets book is mostly
illiquid: a private-equity commitment carries **no** market identifier,
and an ``isin`` column would be structurally NULL for the majority of the
book while conflating "has no identifier" with "not yet imported"
(ADR-0090 §Context / §Alternatives).

Design points carried from ADR-0090 §Decision:

- One row per ``(investment, scheme, value)``. ``scheme`` is plain TEXT
  with a CHECK over the closed set ``('isin','ticker','figi','cusip',
  'internal')`` — no SQL enum, matching the codebase's TEXT-for-status
  convention (b019 precedent). The set is extended only by a successor
  ADR + migration.
- ``value`` is NOT NULL and non-empty after trim (a CHECK guards that);
  no scheme-specific format validation (ISIN checksums etc.) is imposed
  at the DB layer — normalisation and validation are application
  concerns kept off the constraint surface.
- ``tenant_id`` is denormalised onto the row (ADR-0035 §3) so RLS
  evaluates row-locally without a JOIN. ``investment_id`` carries
  ``ON DELETE CASCADE`` so identifiers vanish with their investment;
  ``tenant_id`` and ``created_by`` are ``RESTRICT``, matching the
  investment-domain audit-column idiom (b006).

Three uniqueness rules (all from ADR-0090 §Decision):

1. ``UNIQUE (investment_id, scheme, value)`` — the same identifier is
   not recorded twice for one investment.
2. Partial ``UNIQUE (tenant_id, scheme, value) WHERE scheme <>
   'internal'`` — a real-world security identifier maps to at most one
   investment within a tenant, so two investments cannot claim the same
   ISIN. ``internal`` is exempt because it is a free operator namespace.
3. Partial ``UNIQUE (investment_id) WHERE is_primary`` — at most one
   primary identifier per investment.

The table is tenant-scoped (ADR-0035): the standard
``apply_tenant_rls(...)`` policy is applied, so RLS is enforced through
the application-role switch established in ADR-0078, and the table
satisfies ``tests/regression/test_rls_schema_invariants.py`` without an
allow-list change. No seed rows: this is a domain table with no
per-tenant defaults.

The migration is fully reversible: ``downgrade`` drops the table.
Postgres drops the table's indexes, RLS policy, and row-security state
together with it, so no explicit index/policy drop is required (b019
precedent).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b020_add_investment_identifiers"
down_revision: str | None = "b019_add_irene_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.create_table(
        "investment_identifiers",
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
        sa.Column("scheme", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        # Free-text provenance of the mapping ('excel', 'openfigi',
        # 'manual'); nullable, mirroring investment_navs.source.
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
            "scheme IN ('isin', 'ticker', 'figi', 'cusip', 'internal')",
            name="ck_investment_identifiers_scheme",
        ),
        # Non-emptiness only; no scheme-specific format validation at the
        # DB layer (ADR-0090 §Decision). btrim() catches whitespace-only
        # values as defence in depth behind the repository's normalisation.
        sa.CheckConstraint(
            "char_length(btrim(value)) > 0",
            name="ck_investment_identifiers_value_nonempty",
        ),
        sa.UniqueConstraint(
            "investment_id",
            "scheme",
            "value",
            name="uq_investment_identifiers_investment_scheme_value",
        ),
    )

    # Partial UNIQUE: one real-world identifier maps to at most one
    # investment per tenant. 'internal' is a free operator namespace and
    # is deliberately excluded.
    op.create_index(
        "uq_investment_identifiers_tenant_scheme_value",
        "investment_identifiers",
        ["tenant_id", "scheme", "value"],
        unique=True,
        postgresql_where=sa.text("scheme <> 'internal'"),
    )
    # Partial UNIQUE: at most one primary identifier per investment.
    op.create_index(
        "uq_investment_identifiers_primary_per_investment",
        "investment_identifiers",
        ["investment_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )

    op.execute("SELECT apply_tenant_rls('investment_identifiers');")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Dropping the table drops its indexes, RLS policy, and row-security
    # state with it, so no explicit index/policy drop is required
    # (b019 precedent).
    op.drop_table("investment_identifiers")
