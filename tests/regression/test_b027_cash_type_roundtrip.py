# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b027 (ADR-0100 §1).

Exercises the b027 migration's reversibility: from ``head`` it downgrades to
b026 (narrowing ``ck_investments_investment_type`` back to seven values),
asserts the constraint rejects ``'cash'``, then ``upgrade head`` again and
asserts the constraint once more accepts ``'cash'``. The test always restores
the DB to ``head``.

The downgrade names its **target revision** (b026) rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is, so
a relative downgrade silently stops undoing *this* migration the moment a
newer one lands. Naming ``b026_add_functional_currency_fx`` keeps the guard
asserting what its title claims for every future head.

Because the narrower (seven-value) CHECK revalidates existing rows on
recreation, downgrading with a ``'cash'`` row present fails by design
(ADR-0100 §Implementation Notes). This guard therefore never inserts a cash
row before downgrading; it probes the constraint's *definition* by checking
which literals ``pg_get_constraintdef`` lists, which needs no domain rows at
all and cannot leave a poison row behind.

Runs against a **per-test scratch database**: the ``scratch_db`` fixture
creates one, migrates it to ``head`` and drops it again afterwards. That is
what makes the paragraph above hold in practice as well as in principle — the
shared dev database permanently carries ``'cash'`` rows after any real import,
so this downgrade would fail there for reasons that have nothing to do with
b027 (see ``tests/regression/conftest.py``). The Alembic CLI still runs in a
fresh subprocess, so it does not contend with this test's own connection. If
the server is unreachable the test skips, matching the other live-DB
regression guards.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

_CONSTRAINT = "ck_investments_investment_type"
#: The revision immediately below b027 — i.e. b027's own ``down_revision``.
_BELOW = "b026_add_functional_currency_fx"


async def _constraint_def(engine: AsyncEngine) -> str:
    """Return the human-readable definition of the investment-type CHECK."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
            {"name": _CONSTRAINT},
        )
        value = result.scalar_one_or_none()
    assert value is not None, f"{_CONSTRAINT} not found — is the DB migrated?"
    return value


async def test_b027_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the scratch database is at head, where the CHECK already
    # lists 'cash'.
    assert "cash" in await _constraint_def(scratch_superuser_engine), (
        f"{_CONSTRAINT} does not list 'cash' before round-trip — DB at head?"
    )

    try:
        # 1) downgrade to b026: the narrower CHECK no longer lists 'cash'.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        assert "cash" not in await _constraint_def(scratch_superuser_engine), (
            f"{_CONSTRAINT} still lists 'cash' after downgrade to {_BELOW}"
        )

        # 2) upgrade head → the eighth value is back.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        assert "cash" in await _constraint_def(scratch_superuser_engine), (
            f"{_CONSTRAINT} missing 'cash' after re-upgrade to head"
        )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
