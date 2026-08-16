# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b026 (ADR-0099 §§1–2).

Exercises the b026 migration's reversibility: from ``head`` it downgrades to
b025 (dropping ``fx_rates`` and the ``tenants.functional_currency`` column),
asserts they are gone, then ``upgrade head`` again and asserts the table is
back with RLS enabled + forced and the column is back with its ``'EUR'``
default. The test always restores the DB to ``head``.

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is,
so a relative downgrade silently stops undoing *this* migration the moment a
newer one lands. Naming ``b025_add_system_ingest_origin`` keeps the guard
asserting what its title claims for every future head.

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

_TABLE = "fx_rates"
#: The revision immediately below b026 — i.e. b026's own ``down_revision``.
_BELOW = "b025_add_system_ingest_origin"


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


async def _column_exists(engine: AsyncEngine, table: str, column: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_name = :c"
            ),
            {"t": table, "c": column},
        )
        return result.scalar_one_or_none() is not None


async def _column_default(engine: AsyncEngine, table: str, column: str) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_name = :c"
            ),
            {"t": table, "c": column},
        )
        return result.scalar_one_or_none()


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


async def test_b026_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    assert await _table_exists(scratch_superuser_engine, _TABLE), (
        f"{_TABLE} missing before round-trip — is the DB at head?"
    )
    assert await _column_exists(scratch_superuser_engine, "tenants", "functional_currency"), (
        "tenants.functional_currency missing before round-trip — DB at head?"
    )

    try:
        # 1) downgrade to b025: the table and the column are gone.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        assert not await _table_exists(scratch_superuser_engine, _TABLE), (
            f"{_TABLE} still present after downgrade to {_BELOW}"
        )
        assert not await _column_exists(
            scratch_superuser_engine, "tenants", "functional_currency"
        ), f"tenants.functional_currency still present after downgrade to {_BELOW}"

        # 2) upgrade head → the table is back, RLS enabled + forced, and the
        #    column is back with its retained 'EUR' server default.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        assert await _table_exists(scratch_superuser_engine, _TABLE), (
            f"{_TABLE} missing after re-upgrade to head"
        )
        enabled, forced = await _rls_enabled_and_forced(scratch_superuser_engine, _TABLE)
        assert enabled and forced, (
            f"{_TABLE} must have RLS enabled AND forced after apply_tenant_rls."
        )
        assert await _column_exists(scratch_superuser_engine, "tenants", "functional_currency"), (
            "tenants.functional_currency missing after re-upgrade to head"
        )
        # ADR-0099 §1 keeps the server default (like b024's valuation_mode,
        # unlike b021's ingest_origin): a new tenant is EUR by default.
        default = await _column_default(scratch_superuser_engine, "tenants", "functional_currency")
        assert default is not None and "EUR" in default, (
            f"tenants.functional_currency lost its 'EUR' default: {default!r}"
        )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
