# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b032 (ADR-0112 §2).

Exercises the b032 migration's reversibility: from ``head`` it downgrades to
b031 (dropping ``scoped_settings``), asserts the table is gone, then
``upgrade head`` again and asserts the table is back with the full shape the
ADR specifies — the four CHECKs, the ``NULLS NOT DISTINCT`` unique
constraint, RLS enabled + forced with the standard ``tenant_isolation``
policy, and **no audit trigger** (the deliberate omission: the generic
trigger captures full row images, which would copy every secret's ciphertext
into ``audit_log``). The test always restores the DB to ``head``.

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is, so
a relative downgrade silently stops undoing *this* migration the moment a
newer one lands — it then asserts against someone else's schema. Naming
b032's own ``down_revision`` keeps the guard honest for every future head.

Runs against a **per-test scratch database** (``tests/regression/conftest.py``)
like the other migration guards, so no downgrade ever touches the shared dev
database. If the server is unreachable the test skips.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

_TABLE = "scoped_settings"
#: The revision immediately below b032 — i.e. b032's own ``down_revision``.
_BELOW = "b031_add_case_workflow"

_EXPECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "scope",
        "tenant_id",
        "user_id",
        "provider",
        "key",
        "is_secret",
        "value_plain",
        "value_ciphertext",
        "secret_hint",
        "enabled",
        "created_at",
        "updated_at",
    }
)

_EXPECTED_CHECKS = (
    "ck_scoped_settings_scope_vocabulary",
    "ck_scoped_settings_application_scope_null_tenant",
    "ck_scoped_settings_user_scope_requires_user",
    "ck_scoped_settings_secret_value_exclusivity",
)

_UNIQUE_CONSTRAINT = "uq_scoped_settings_scope_tenant_user_provider_key"


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


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        return {row[0] for row in result.fetchall()}


async def _constraint_names(engine: AsyncEngine, table: str, contype: str) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND t.relname = :t
                  AND c.contype::text = :contype
                """
            ),
            {"t": table, "contype": contype},
        )
        return {row[0] for row in result.fetchall()}


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


async def _trigger_count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT count(*)
                FROM pg_trigger tg
                JOIN pg_class t ON tg.tgrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND t.relname = :t
                  AND NOT tg.tgisinternal
                """
            ),
            {"t": table},
        )
        return int(result.scalar_one())


async def _unique_is_nulls_not_distinct(engine: AsyncEngine, constraint: str) -> bool:
    """Read ``indnullsnotdistinct`` off the constraint's backing index."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT i.indnullsnotdistinct
                FROM pg_constraint c
                JOIN pg_index i ON i.indexrelid = c.conindid
                WHERE c.conname = :name
                """
            ),
            {"name": constraint},
        )
        return bool(result.scalar_one())


async def test_b032_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    assert await _table_exists(scratch_superuser_engine, _TABLE), (
        f"{_TABLE} missing before round-trip — is the DB at head?"
    )

    try:
        # 1) downgrade to b031: the table is gone.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        assert not await _table_exists(scratch_superuser_engine, _TABLE), (
            f"{_TABLE} still present after downgrade to {_BELOW}"
        )

        # 2) upgrade head → the table is back, in full shape.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        assert await _table_exists(scratch_superuser_engine, _TABLE), (
            f"{_TABLE} missing after re-upgrade to head"
        )

        columns = await _columns(scratch_superuser_engine, _TABLE)
        assert columns == _EXPECTED_COLUMNS, (
            f"{_TABLE} column set drifted from the ADR-0112 §2 shape: "
            f"missing={sorted(_EXPECTED_COLUMNS - columns)}, "
            f"extra={sorted(columns - _EXPECTED_COLUMNS)}"
        )

        checks = await _constraint_names(scratch_superuser_engine, _TABLE, "c")
        for name in _EXPECTED_CHECKS:
            assert name in checks, f"{_TABLE} is missing the {name} CHECK."

        uniques = await _constraint_names(scratch_superuser_engine, _TABLE, "u")
        assert _UNIQUE_CONSTRAINT in uniques, (
            f"{_TABLE} is missing the {_UNIQUE_CONSTRAINT} unique constraint."
        )
        assert await _unique_is_nulls_not_distinct(scratch_superuser_engine, _UNIQUE_CONSTRAINT), (
            f"{_UNIQUE_CONSTRAINT} must be NULLS NOT DISTINCT (ADR-0112 §2) — "
            "otherwise two tenant-scope rows with the same provider/key and a "
            "NULL user_id would both be admitted."
        )

        enabled, forced = await _rls_enabled_and_forced(scratch_superuser_engine, _TABLE)
        assert enabled and forced, (
            f"{_TABLE} must have RLS enabled AND forced after apply_tenant_rls."
        )
        assert await _has_tenant_isolation_policy(scratch_superuser_engine, _TABLE), (
            f"{_TABLE} is missing the standard tenant_isolation policy."
        )

        assert await _trigger_count(scratch_superuser_engine, _TABLE) == 0, (
            f"{_TABLE} must carry no audit trigger: the generic trigger captures "
            "full row images (to_jsonb(NEW)/to_jsonb(OLD)), which would copy every "
            "secret's ciphertext and hint into audit_log — contradicting "
            "ADR-0112 §6."
        )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
