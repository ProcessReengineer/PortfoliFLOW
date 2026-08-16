# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b022 (ADR-0093).

Exercises the b022 migration's reversibility: from ``head`` it runs
a downgrade to b021 (dropping ``market_data_schedule``), asserts
the table is gone, then ``upgrade head`` again and asserts it is back with
its RLS enabled/forced. The test always restores the DB to ``head``.

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

_TABLE = "market_data_schedule"
#: The revision immediately below b022 — i.e. b022's own ``down_revision``.
#: Named rather than using ``downgrade -1``, which is relative to the DB's
#: current head and therefore undoes a *newer* migration once one lands.
_BELOW = "b021_add_ingest_origin"


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        return result.scalar_one_or_none() is not None


async def _rls_enabled_and_forced(engine: AsyncEngine, table: str) -> tuple[bool, bool]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t AND relnamespace = 'public'::regnamespace"
                ),
                {"t": table},
            )
        ).first()
    assert row is not None
    return bool(row.relrowsecurity), bool(row.relforcerowsecurity)


async def test_b022_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    assert await _table_exists(scratch_superuser_engine, _TABLE), (
        f"{_TABLE} missing before round-trip — is the DB at head?"
    )

    try:
        # 1) downgrade to b021: the table is gone.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        assert not await _table_exists(scratch_superuser_engine, _TABLE), (
            f"{_TABLE} still present after downgrade to {_BELOW}"
        )

        # 2) upgrade head → the table is back, with RLS enabled + forced.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        assert await _table_exists(scratch_superuser_engine, _TABLE), (
            f"{_TABLE} missing after re-upgrade to head"
        )
        enabled, forced = await _rls_enabled_and_forced(scratch_superuser_engine, _TABLE)
        assert enabled and forced, (
            f"{_TABLE} must have RLS enabled AND forced after apply_tenant_rls."
        )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
