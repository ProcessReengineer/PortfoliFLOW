# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b031 (ADR-0107).

Exercises the b031 migration's reversibility: from ``head`` it downgrades to
b030 (dropping ``cases``, ``case_entries`` and ``case_attachments``), asserts
they are gone, then ``upgrade head`` again and asserts the three tables are
back with RLS enabled + forced, the standard ``tenant_isolation`` policy
attached, and the ``uq_cases_tenant_case_number`` constraint in place. The
test always restores the DB to ``head``.

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is, so
a relative downgrade silently stops undoing *this* migration the moment a
newer one lands — it then asserts against someone else's schema. Naming
b031's own ``down_revision`` keeps the guard honest for every future head.

Runs against a **per-test scratch database**: the ``scratch_db`` fixture
creates one, migrates it to ``head`` and drops it again afterwards. This guard
in particular *must* not run against the shared dev database: its downgrade
drops the three case tables, which would destroy any real Cases data there
(see ``tests/regression/conftest.py``). The Alembic CLI still runs in a fresh
subprocess, so it does not contend with this test's own connection. If the
server is unreachable the test skips, matching the other live-DB regression
guards.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

_TABLES = ("cases", "case_entries", "case_attachments")
#: The revision immediately below b031 — i.e. b031's own ``down_revision``.
_BELOW = "b030_drop_portfolio_aum"


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


async def _has_tenant_isolation_policy(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = :t "
                "AND policyname = 'tenant_isolation'"
            ),
            {"t": table},
        )
        return result.scalar_one_or_none() is not None


async def _has_constraint(engine: AsyncEngine, table: str, name: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT 1
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND t.relname = :t
                  AND c.conname = :name
                """
            ),
            {"t": table, "name": name},
        )
        return result.scalar_one_or_none() is not None


async def test_b031_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    for table in _TABLES:
        assert await _table_exists(scratch_superuser_engine, table), (
            f"{table} missing before round-trip — is the DB at head?"
        )

    try:
        # 1) downgrade to b030: the three tables are gone.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        for table in _TABLES:
            assert not await _table_exists(scratch_superuser_engine, table), (
                f"{table} still present after downgrade to {_BELOW}"
            )

        # 2) upgrade head → the tables are back, RLS enabled + forced, the
        #    tenant_isolation policy attached, and the case-number unique
        #    constraint in place.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        for table in _TABLES:
            assert await _table_exists(scratch_superuser_engine, table), (
                f"{table} missing after re-upgrade to head"
            )
            enabled, forced = await _rls_enabled_and_forced(scratch_superuser_engine, table)
            assert enabled and forced, (
                f"{table} must have RLS enabled AND forced after apply_tenant_rls."
            )
            assert await _has_tenant_isolation_policy(scratch_superuser_engine, table), (
                f"{table} is missing the standard tenant_isolation policy."
            )
        assert await _has_constraint(
            scratch_superuser_engine, "cases", "uq_cases_tenant_case_number"
        ), (
            "cases is missing the uq_cases_tenant_case_number unique "
            "constraint after re-upgrade to head."
        )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
