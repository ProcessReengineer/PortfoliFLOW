# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add the 'cash' investment type (ADR-0100 §1).

Revision ID: b027_add_cash_investment_type
Revises: b026_add_functional_currency_fx
Create Date: 2026-07-11 12:00:00 UTC

The schema landing of ADR-0100: ``'cash'`` becomes the eighth
``investments.investment_type`` value, so a foreign-currency cash balance
can be a first-class ``investments`` row (converted, limit-checked and
AnlV-classified through the ADR-0099 seam) rather than being folded into
the functional-currency residual it structurally cannot represent.

ADR-0055 rejected "cash as a first-class row" partly on the cost of this
very CHECK migration; ADR-0100 pays it deliberately. The change is a
pure constraint widening:

* ``upgrade`` drops ``ck_investments_investment_type`` and recreates it
  with the seven existing values **plus** ``'cash'``. No data migration
  and no default rows are created — a tenant models cash explicitly only
  when it actually holds a foreign-currency balance (ADR-0100 §2).
* ``downgrade`` restores the seven-value CHECK. This is reversible **only
  while no ``'cash'`` rows exist**: recreating the narrower constraint
  validates existing rows, so a downgrade with cash positions present
  fails on the CHECK by design (there is no lossy silent conversion of a
  cash row back to one of the seven private/liquid types). The b027
  round-trip guard therefore downgrades against a cash-free table.

Widening a CHECK is non-destructive to existing rows (every pre-existing
value still satisfies the larger set), so ``upgrade`` needs no table
rewrite beyond the constraint revalidation Postgres performs on ADD.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b027_add_cash_investment_type"
down_revision: str | None = "b026_add_functional_currency_fx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = "ck_investments_investment_type"

# Seven canonical types (pre-ADR-0100) and the eighth ('cash') added here.
_SEVEN = (
    "'private_equity', 'private_debt', 'real_estate', "
    "'infra_equity', 'listed_equity', 'listed_bonds', 'other'"
)
_EIGHT = _SEVEN + ", 'cash'"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "investments", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "investments",
        f"investment_type IN ({_EIGHT})",
    )


def downgrade() -> None:
    # Reversible only while no 'cash' rows exist — recreating the narrower
    # CHECK revalidates every row, so a table holding cash positions fails
    # here by design (ADR-0100 §Implementation Notes: no lossy back-cast).
    op.drop_constraint(_CONSTRAINT, "investments", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "investments",
        f"investment_type IN ({_SEVEN})",
    )
