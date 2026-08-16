# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add ingest_origin (+ cashflow source) — the Excel-precedence field.

Revision ID: b021_add_ingest_origin
Revises: b020_add_investment_identifiers
Create Date: 2026-07-06 12:30:00 UTC

The fourth implementation slice of Live Data Import (roadmap #036), per
ADR-0092. Live import is a **second producer** into the same target
tables the Excel extractor already writes; this migration adds the
typed ``ingest_origin`` field the Excel-precedence guard decides on, and
the free-text ``source`` column the live cashflow write path records its
provider in.

Two schema additions:

1. ``ingest_origin TEXT NOT NULL`` with CHECK
   ``ingest_origin IN ('excel','live','manual')`` on all **seven**
   ingested tables — ``investment_navs``, ``investment_cashflows`` and
   the five historised composition-weight tables
   (``investment_region_weights``, ``investment_country_weights``,
   ``investment_sector_weights``, ``investment_rating_weight``,
   ``investment_maturity_weight``). Existing rows backfill to
   ``'excel'`` (their true origin — a *definite* backfill, unlike the
   nullable ``basis``, ADR-0092 §Decision). The per-table sequence is:
   add nullable with server default ``'excel'`` (which backfills every
   existing row) → set NOT NULL → **drop the server default** so every
   application write must state its origin explicitly (there is no DB
   default to fall back on) → add the CHECK.

2. ``investment_cashflows.source TEXT`` nullable (mirroring
   ``investment_navs.source``). No backfill — NULL is the honest
   historical value for pre-live cashflows (operator-approved
   resolution of the ADR gap: ADR-0092's cashflow dedup key names
   ``source`` but the table lacked the column). The five weight tables
   deliberately do **not** get ``source``: precedence needs only
   ``ingest_origin`` there and no current provider serves weights
   (ADR-0091 capability matrix).

``investment_region_weights`` has no ``updated_at`` column by design
(ADR-0080 §Scope boundaries); this migration does not add one — it
touches only what the precedence guard needs.

The migration is fully reversible: ``downgrade`` drops the added CHECK
constraints and columns (Postgres drops a column's CHECK with the
column, but the constraints are dropped explicitly first for clarity /
symmetry with the sibling migrations). RLS policies, indexes and audit
triggers on all seven tables survive ``ADD COLUMN`` untouched and are
deliberately not re-applied (b017 precedent).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b021_add_ingest_origin"
down_revision: str | None = "b020_add_investment_identifiers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The seven tables that gain ``ingest_origin``. Order is NAV → cashflow
# → the five weight families; independent tables, so the order is
# cosmetic.
_INGEST_ORIGIN_TABLES: tuple[str, ...] = (
    "investment_navs",
    "investment_cashflows",
    "investment_region_weights",
    "investment_country_weights",
    "investment_sector_weights",
    "investment_rating_weight",
    "investment_maturity_weight",
)


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    for table in _INGEST_ORIGIN_TABLES:
        # 1. Add nullable with server default 'excel'. Adding a column
        #    with a DEFAULT populates every existing row with that value
        #    in one statement — the definite backfill ADR-0092 mandates.
        op.add_column(
            table,
            sa.Column(
                "ingest_origin",
                sa.Text(),
                nullable=True,
                server_default=sa.text("'excel'"),
            ),
        )
        # 2. Every row now carries 'excel'; promote to NOT NULL.
        op.alter_column(
            table,
            "ingest_origin",
            existing_type=sa.Text(),
            nullable=False,
        )
        # 3. Drop the server default so every application write must
        #    state its origin explicitly (no DB fallback exists after
        #    this point — ADR-0092 §Decision).
        op.alter_column(
            table,
            "ingest_origin",
            existing_type=sa.Text(),
            server_default=None,
        )
        # 4. Constrain to the closed producer set.
        op.create_check_constraint(
            f"ck_{table}_ingest_origin",
            table,
            "ingest_origin IN ('excel', 'live', 'manual')",
        )

    # 5. Cashflows gain the free-text provenance column the live write
    #    path records its provider in. Nullable, no backfill — NULL is
    #    the honest historical value (ADR-0092 §0.1 resolution).
    op.add_column(
        "investment_cashflows",
        sa.Column("source", sa.Text(), nullable=True),
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    op.drop_column("investment_cashflows", "source")
    for table in reversed(_INGEST_ORIGIN_TABLES):
        op.drop_constraint(f"ck_{table}_ingest_origin", table, type_="check")
        op.drop_column(table, "ingest_origin")
