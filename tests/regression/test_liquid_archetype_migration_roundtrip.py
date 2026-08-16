# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for the ADR-0079 liquid-archetype tables.

Exercises the b016 migration's reversibility: from ``head`` it downgrades
to b015 (dropping the three new tables and the ``investment_navs.basis``
column) then ``upgrade head`` again, and asserts the schema artefacts are
present after the final upgrade.

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is,
so a relative downgrade silently stops undoing *this* migration the moment
a newer one lands — it then asserts against someone else's schema. Naming
b016's own ``down_revision`` keeps the guard honest for every future head.
The test restores the DB to ``head`` in a ``finally`` so that a mid-test
failure cannot strand the schema.

Runs against a **per-test scratch database**: the ``scratch_db`` fixture
creates one, migrates it to ``head`` and drops it again afterwards, so this
guard's downgrade never reaches the shared dev database (see
``tests/regression/conftest.py`` for why that matters). The Alembic CLI still
runs in a fresh subprocess, so it does not contend with this test's own
connection. If the server is unreachable the test skips, matching the other
live-DB regression guards.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

#: The revision immediately below b016 — i.e. b016's own ``down_revision``.
_BELOW = "b015_add_user_display_name"

_NEW_TABLES = (
    "investment_bond_analytics",
    "investment_rating_weight",
    "investment_maturity_weight",
)


async def _table_exists(engine: AsyncEngine, name: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT to_regclass('public.' || :name)"),
            {"name": name},
        )
        return result.scalar_one() is not None


async def _navs_has_basis(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'investment_navs'
                  AND column_name = 'basis'
                """
            )
        )
        return result.scalar_one_or_none() is not None


async def test_b016_downgrade_upgrade_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    for table in _NEW_TABLES:
        assert await _table_exists(scratch_superuser_engine, table), (
            f"{table} missing before round-trip — is the DB at head?"
        )

    try:
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"

        # After downgrade the new tables and the basis column are gone.
        for table in _NEW_TABLES:
            assert not await _table_exists(scratch_superuser_engine, table), (
                f"{table} still present after downgrade to {_BELOW}"
            )
        assert not await _navs_has_basis(scratch_superuser_engine), (
            f"investment_navs.basis still present after downgrade to {_BELOW}"
        )

        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"

        # After re-upgrade everything is back.
        for table in _NEW_TABLES:
            assert await _table_exists(scratch_superuser_engine, table), (
                f"{table} missing after re-upgrade to head"
            )
        assert await _navs_has_basis(scratch_superuser_engine), (
            "investment_navs.basis missing after re-upgrade to head"
        )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
