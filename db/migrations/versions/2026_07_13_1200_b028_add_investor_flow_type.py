# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add the 'investor_flow' cashflow type (ADR-0103 §5).

Revision ID: b028_add_investor_flow_type
Revises: b027_add_cash_investment_type
Create Date: 2026-07-13 12:00:00 UTC

The first schema landing of ADR-0103: ``'investor_flow'`` becomes the
eighth ``investment_cashflows.flow_type`` value — net contributions to and
withdrawals from the mandate, booked on the **cash position of the currency
they settle in** (ADR-0103 §5, decision N4). The cash-only booking rule is
a service-level validation
(:meth:`services.investments.InvestmentService.add_cashflow`), not a DB
constraint: it spans two tables (``investment_cashflows.flow_type`` and
``investments.investment_type``) and a CHECK cannot see across the FK.

Following the b027 idiom, the change is a pure constraint widening:

* ``upgrade`` drops ``ck_investment_cashflows_flow_type`` and recreates it
  with the seven existing values **plus** ``'investor_flow'``. No data
  migration and no rows are created — an investor flow exists only once a
  tenant books one.
* ``downgrade`` restores the seven-value CHECK. This is reversible **only
  while no ``'investor_flow'`` rows exist**: recreating the narrower
  constraint revalidates every row, so a downgrade with investor flows
  present fails on the CHECK by design (there is no lossy silent re-cast of
  an investor flow to one of the seven original types — it is neither a
  call, nor a distribution, nor an ``'other'``). The b028 round-trip guard
  therefore downgrades against an investor-flow-free table.

Widening a CHECK is non-destructive to existing rows (every pre-existing
value still satisfies the larger set), so ``upgrade`` needs no table rewrite
beyond the constraint revalidation Postgres performs on ADD.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b028_add_investor_flow_type"
down_revision: str | None = "b027_add_cash_investment_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = "ck_investment_cashflows_flow_type"

# Seven canonical flow types (pre-ADR-0103) and the eighth
# ('investor_flow') added here.
_SEVEN = "'capital_call', 'distribution', 'fee', 'carry', 'dividend', 'coupon', 'other'"
_EIGHT = _SEVEN + ", 'investor_flow'"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "investment_cashflows", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "investment_cashflows",
        f"flow_type IN ({_EIGHT})",
    )


def downgrade() -> None:
    # Reversible only while no 'investor_flow' rows exist — recreating the
    # narrower CHECK revalidates every row, so a table holding investor
    # flows fails here by design (no lossy back-cast: an investor flow is
    # not a call, not a distribution, not an 'other').
    op.drop_constraint(_CONSTRAINT, "investment_cashflows", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "investment_cashflows",
        f"flow_type IN ({_SEVEN})",
    )
