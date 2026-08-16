# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b025 (ADR-0098 §1).

Exercises the b025 migration's reversibility: from ``head`` it asserts the
``investment_navs.ingest_origin`` CHECK admits ``'system'``, downgrades to
b024 (re-narrowing the CHECK to the b021 triple), asserts ``'system'`` is
gone, then ``upgrade head`` again and asserts it is back. The test always
restores the DB to ``head``.

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is,
so a relative downgrade stops undoing *this* migration the moment a newer
one lands (b026 did exactly that). Naming ``b024_add_position_model`` keeps
the guard asserting what its title claims for every future head.

Runs against a **per-test scratch database**: the ``scratch_db`` fixture
creates one, migrates it to ``head`` and drops it again afterwards, so this
guard's downgrade never reaches the shared dev database (see
``tests/regression/conftest.py`` for why that matters). The Alembic CLI still
runs in a fresh subprocess, so it does not contend with this test's own
connection. If the server is unreachable the test skips, matching the other
live-DB migration guards.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

_CONSTRAINT = "ck_investment_navs_ingest_origin"
#: The revision immediately below b025 — i.e. b025's own ``down_revision``.
_BELOW = "b024_add_position_model"


async def _constraint_admits_system(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        definition = (
            await conn.execute(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
                {"name": _CONSTRAINT},
            )
        ).scalar_one_or_none()
    assert definition is not None, f"{_CONSTRAINT} not found on the DB."
    return "system" in definition


async def test_b025_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the scratch database is at head — 'system' is admitted.
    assert await _constraint_admits_system(scratch_superuser_engine), (
        "'system' not admitted before round-trip — is the DB at head?"
    )

    try:
        # 1) downgrade to b024: the CHECK re-narrows to the b021 triple.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        assert not await _constraint_admits_system(scratch_superuser_engine), (
            f"'system' still admitted after downgrade to {_BELOW}"
        )

        # 2) upgrade head → 'system' is admitted again.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        assert await _constraint_admits_system(scratch_superuser_engine), (
            "'system' not admitted after re-upgrade to head"
        )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
