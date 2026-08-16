# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Extend investment_navs.ingest_origin with the 'system' writer channel.

Revision ID: b025_add_system_ingest_origin
Revises: b024_add_position_model
Create Date: 2026-07-08 13:00:00 UTC

The materialisation layer of the unitised position model (roadmap #038
strand S2), per ADR-0098 §1. The computed-NAV materialisation service
writes ``holdings × price`` results into ``investment_navs`` as ordinary
``actual`` rows so every consumer of the NAV-series contract works
unchanged (ADR-0098 finding F3). Those rows need a producer marker of
their own: they are neither imported (``'excel'``), provider-delivered
(``'live'``), nor operator-entered (``'manual'``).

This migration makes exactly one schema change — the closed-set
extension ADR-0098 §1 pins:

    ingest_origin IN ('excel','live','manual')
      →
    ingest_origin IN ('excel','live','manual','system')

``'system'`` marks a row written by the platform's materialisation
service. It stays **orthogonal** to ``basis`` (``basis='computed'`` says
*how* the number was formed, ADR-0079; ``ingest_origin='system'`` says
*which writer* produced it), and to the free-text ``source``. Precedence,
strongest first, is ``'excel'`` > ``'manual'`` > ``'system'`` — the
materialisation refreshes only its own ``'system'`` rows (the
``upsert_computed`` guard) and never mutates an ``'excel'``/``'manual'``
row. One concern per migration: ADR-0097's tables landed in ``b024``;
this ADR's origin extension is its own slot.

The change is a CHECK-constraint swap: the b021 constraint
``ck_investment_navs_ingest_origin`` is dropped and recreated with the
wider set. No column, index, RLS policy or audit trigger on
``investment_navs`` is touched (they survive the constraint swap), and
``upsert_live`` / the ADR-0092 semantics are unchanged.

Reversibility: ``downgrade`` re-narrows the CHECK to the b021 triple.
Because a re-narrowed CHECK is validated against existing rows, any
``'system'`` rows present would block it — so ``downgrade`` first
**deletes** the ``'system'`` rows. That is safe and honest: computed
rows are a derived read product (holdings × price), fully
re-materialisable by re-running the service after a subsequent upgrade;
they carry no book-of-record information. Only ``'system'``-origin rows
are removed — ``'excel'``, ``'manual'`` and ``'live'`` rows are left
untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b025_add_system_ingest_origin"
down_revision: str | None = "b024_add_position_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT: str = "ck_investment_navs_ingest_origin"
_TABLE: str = "investment_navs"

_WIDE_SET: str = "ingest_origin IN ('excel', 'live', 'manual', 'system')"
_NARROW_SET: str = "ingest_origin IN ('excel', 'live', 'manual')"


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # Swap the closed producer set: drop the b021 CHECK and recreate it
    # with 'system' added. Postgres validates the new CHECK against every
    # existing row; all existing origins are within the wider set, so the
    # recreation always succeeds.
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WIDE_SET)


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Re-narrowing the CHECK would fail if any 'system' rows remained (the
    # constraint is validated against existing data). Computed rows are a
    # derived, re-materialisable read product, so deleting them is safe and
    # is the honest reverse of introducing the 'system' channel. Only
    # 'system'-origin rows are removed.
    op.execute("DELETE FROM investment_navs WHERE ingest_origin = 'system'")
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _NARROW_SET)
