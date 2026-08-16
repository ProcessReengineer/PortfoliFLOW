# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Historise the composition-weight tables (sector / region / country).

Revision ID: b017_historise_composition_wts
Revises: b016_add_liquid_archetypes
Create Date: 2026-06-15 13:00:00 UTC

Implements ADR-0080 §1–2 (migration half). The three legacy
point-in-time composition-weight tables —
``investment_sector_weights``, ``investment_region_weights`` and
``investment_country_weights`` — are promoted to **time-series**,
aligned to the ADR-0079 weight pattern. Each table:

1. drops its old point-in-time unique constraint;
2. gains ``as_of_date DATE`` and ``basis TEXT`` (added nullable for
   backfill);
3. backfills ``basis = 'reported'`` for every existing row;
4. backfills ``as_of_date`` from the **latest ``actual`` NAV** of the
   same investment (ADR-0080 §2 — the honest "this is what last held"
   date);
5. is guarded by a hard integrity gate that **aborts loudly**, naming
   the offending ``investment_id``s, if any row could not be anchored
   (no silent sentinel fallback);
6. makes both new columns NOT NULL;
7. gains a ``basis IN ('reported','computed')`` CHECK; and
8. gains the new three-column unique constraint carrying
   ``as_of_date`` in the natural key.

Migrations run under the privileged connection with RLS bypassed;
``investment_id`` is a global PK so the NAV join is tenant-correct
without an ``app.tenant_id`` GUC.

RLS policies, audit triggers and the ``ix_*`` indices already exist on
all three tables (b007/b009) and survive ``ADD COLUMN`` untouched —
they are deliberately **not** re-applied here.

The dual Alembic head condition (``b013_super_admin_audit`` and
``b016_add_liquid_archetypes``) is pre-existing and unchanged; this
migration chains off ``b016`` and introduces no merge. Because of the
two heads the upgrade is addressed by explicit revision rather than
``upgrade head``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b017_historise_composition_wts"
down_revision: str | None = "b016_add_liquid_archetypes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Per-table configuration. Order is sector → region → country; the
# tables are independent so the order is cosmetic, but it matches the
# ADR-0080 §1 table for readability.
_TABLES: tuple[dict[str, object], ...] = (
    {
        "table": "investment_sector_weights",
        "dim": "sector_id",
        "old_uq": "uq_investment_sector_weights_investment_sector",
        "old_uq_cols": ["investment_id", "sector_id"],
        "new_uq": "uq_investment_sector_weights_investment_date_sector",
    },
    {
        "table": "investment_region_weights",
        "dim": "region_id",
        "old_uq": "uq_investment_region_weights_inv_region_unique",
        # The region table's original natural key carried tenant_id.
        "old_uq_cols": ["tenant_id", "investment_id", "region_id"],
        "new_uq": "uq_investment_region_weights_investment_date_region",
    },
    {
        "table": "investment_country_weights",
        "dim": "country_iso_code",
        "old_uq": "uq_investment_country_weights_investment_country",
        "old_uq_cols": ["investment_id", "country_iso_code"],
        "new_uq": "uq_investment_country_weights_investment_date_country",
    },
)


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    for cfg in _TABLES:
        table = str(cfg["table"])
        dim = str(cfg["dim"])
        old_uq = str(cfg["old_uq"])
        new_uq = str(cfg["new_uq"])

        # 1. Drop the old point-in-time unique constraint.
        op.drop_constraint(old_uq, table, type_="unique")

        # 2. Add the historisation columns nullable, ready for backfill.
        op.add_column(table, sa.Column("as_of_date", sa.Date(), nullable=True))
        op.add_column(table, sa.Column("basis", sa.Text(), nullable=True))

        # 3. Backfill basis — every legacy row is reported provenance.
        op.execute(f"UPDATE {table} SET basis = 'reported' WHERE basis IS NULL")

        # 4. Backfill as_of_date from the latest actual NAV per investment.
        op.execute(
            f"""
            UPDATE {table} AS w
            SET as_of_date = sub.max_actual
            FROM (
                SELECT investment_id, MAX(as_of_date) AS max_actual
                FROM investment_navs
                WHERE nav_kind = 'actual'
                GROUP BY investment_id
            ) AS sub
            WHERE sub.investment_id = w.investment_id
              AND w.as_of_date IS NULL
            """
        )

        # 5. Integrity gate — abort loudly if any row could not be
        #    anchored. A sentinel fallback is explicitly rejected
        #    (ADR-0080 §2, no-silent-fallback).
        op.execute(
            f"""
            DO $$
            DECLARE offending text;
            BEGIN
              SELECT string_agg(DISTINCT investment_id::text, ', ')
                INTO offending
                FROM {table}
               WHERE as_of_date IS NULL;
              IF offending IS NOT NULL THEN
                RAISE EXCEPTION
                  'ADR-0080 backfill: {table} rows reference investments '
                  'with no actual NAV to anchor as_of_date: %', offending;
              END IF;
            END $$;
            """
        )

        # 6. Promote the backfilled columns to NOT NULL.
        op.alter_column(
            table,
            "as_of_date",
            existing_type=sa.Date(),
            nullable=False,
        )
        op.alter_column(
            table,
            "basis",
            existing_type=sa.Text(),
            nullable=False,
        )

        # 7. Constrain basis to the canonical discriminator values.
        op.create_check_constraint(
            f"ck_{table}_basis",
            table,
            "basis IN ('reported', 'computed')",
        )

        # 8. Create the new three-column natural key carrying as_of_date.
        op.create_unique_constraint(
            new_uq,
            table,
            ["investment_id", "as_of_date", dim],
        )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Exact inverse per table. NOTE: the original (as_of_date-less) row
    # generations cannot be perfectly reconstructed — promoting
    # as_of_date into the natural key is information-additive and the
    # backfill is lossy in reverse. This is acceptable for a dev-only
    # downgrade; production never rolls this back.
    for cfg in reversed(_TABLES):
        table = str(cfg["table"])
        old_uq = str(cfg["old_uq"])
        old_uq_cols = list(cfg["old_uq_cols"])  # type: ignore[arg-type]
        new_uq = str(cfg["new_uq"])

        op.drop_constraint(new_uq, table, type_="unique")
        op.drop_constraint(f"ck_{table}_basis", table, type_="check")
        op.drop_column(table, "basis")
        op.drop_column(table, "as_of_date")
        op.create_unique_constraint(old_uq, table, old_uq_cols)
