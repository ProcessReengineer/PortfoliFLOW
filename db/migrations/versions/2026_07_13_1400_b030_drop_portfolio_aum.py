# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Drop portfolio_aum — AUM is Σ NAV, not a persisted series.

Revision ID: b030_drop_portfolio_aum
Revises: b029_migrate_cash_to_unitised
Create Date: 2026-07-13 14:00:00 UTC

ADR-0103 §2/§7, the final step of the cash strand. The ``portfolio_aum``
table (b010, ADR-0055) modelled AUM as an independently persisted daily
series, against which cash was the *residual* ``aum_eur − Σ nav``. ADR-0103
retires both. Cash is an investment now (§1), so the book is complete, and
AUM is defined uniformly as

    aum(t) = Σ nav_functional(t)        (all investments, incl. cash rows)

There is no unmodelled float: **what is not on a statement does not exist for
the platform.** A derived denominator cannot go stale against the numerators
it is derived from, which is the whole point — the residual's failure mode
(an under-stated AUM row silently producing negative cash) has no equivalent
left to guard against, and the ADR-0055/0067 negative-suppression rule
retires with it.

**Ordering (annex §A.3, binding).** This drop runs *after* cash rows
materialise correctly and reconcile once — never before. That gate is the
operator's, and it was cleared before this migration was written.

**What goes with the table.** ``DROP TABLE`` takes the RLS policies, the
row-security state and the ``portfolio_aum_audit_trigger`` with it; nothing
needs dropping by hand (the b024 precedent). ``audit_log`` rows the trigger
wrote in the past are *not* touched — the audit trail is history, and history
does not retract because its subject was retired.

**Data loss, stated plainly.** The rows are unrecoverable after this. Two
things make that acceptable rather than merely tolerable:

1. Since ADR-0103 §3 (strand S1.3) the import path has written **no**
   ``portfolio_aum`` row at all — the ``AUM`` sheet was demoted to a
   reconciliation control that persists nothing. Whatever is in the table is
   pre-S1.3 residue, already superseded by the Σ-NAV definition every surface
   now reads.
2. The sheet the rows came from is the workbook's, and the workbook is the
   operator's. Re-importing it re-runs the *control*, not a write.

``downgrade()`` therefore recreates the table **empty**, exactly per its b010
DDL (restated verbatim below so this migration is self-contained and does not
import a sibling revision's private helpers). It restores the *schema*, not
the data — documented one-way residue in the b028/b029 tradition. A downgrade
leaves a tenant with an empty AUM series, which the pre-ADR-0103 code read as
"no AUM data" — the empty-state path, not a corrupt one. A roundtrip guard
test is deliberately omitted: the downgrade recreates schema only and the data
is unrestorable, so a downgrade/upgrade cycle has no invariant worth pinning.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b030_drop_portfolio_aum"
down_revision: str | None = "b029_migrate_cash_to_unitised"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop ``portfolio_aum`` (policies, RLS state and audit trigger with it)."""
    op.drop_table("portfolio_aum")


def downgrade() -> None:
    """Recreate ``portfolio_aum`` — **empty**, per its original b010 DDL.

    Schema-only restoration: the dropped rows are gone (see the module
    docstring). The RLS policies and the audit trigger are re-installed the
    same way b010 installed them, so a downgraded database is structurally
    indistinguishable from a pre-b030 one.
    """
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
