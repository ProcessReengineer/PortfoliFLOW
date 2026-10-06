# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b036 (ADR-0133 §1.1, §2.1, §3.1).

Exercises the b036 migration's reversibility: from ``head`` it downgrades
to b034 (dropping ``irene_briefing``, ``watch_family_scope`` and the six
``floor_calibration.warn_pct_<family>`` columns), asserts all of it is
gone, then ``upgrade head`` again and asserts it is back in the shape the
ADR specifies — the column sets, the CHECKs that pin the briefing shape
and the scope vocabulary, both unique constraints, RLS enabled + forced
with the standard ``tenant_isolation`` policy, and the six WARN columns
as nullable ``numeric(6,3)``.

Three absences get their own assertions, because an absence is exactly
what a well-meaning later migration "completes":

1. **No ``updated_at`` on either table.** Both are append-only: a
   briefing row is immutable, a scope change inserts a later revision.
2. **No audit trigger on ``irene_briefing``**, while
   ``watch_family_scope`` carries exactly one. A briefing is a system
   artefact with no human actor; switching a family off is an owner's
   decision that needs actor attribution (the ADR-0116 §1 model).
3. **No ``warn_pct_rss``.** A press-coverage cluster has no scalar
   magnitude to measure a WARN fraction against (D-WD-13).

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is,
so a relative downgrade silently stops undoing *this* migration the moment
a newer one lands. Naming b036's own ``down_revision`` keeps the guard
honest for every future head.

Runs against a **per-test scratch database** (``tests/regression/conftest.py``)
like the other migration guards, so no downgrade ever touches the shared
dev database. If the server is unreachable the test skips.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

_BRIEFING = "irene_briefing"
_SCOPE = "watch_family_scope"
_CALIBRATION = "floor_calibration"
#: The revision immediately below b036 — i.e. b036's own ``down_revision``.
_BELOW = "b034_add_trade_tickets"

_EXPECTED_BRIEFING_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "beat_at",
        "written_at",
        "status",
        "skip_reason",
        "text_markets",
        "text_watch",
        "inputs",
        "finding_ids",
        "model_id",
        "created_at",
    }
)

_EXPECTED_SCOPE_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "family",
        "effective_from",
        "enabled",
        "preset",
        "changed_by",
        "note",
        "created_at",
    }
)

_WARN_COLUMNS: tuple[str, ...] = (
    "warn_pct_saa",
    "warn_pct_anlv",
    "warn_pct_price",
    "warn_pct_fx",
    "warn_pct_freshness",
    "warn_pct_liquidity",
)

_EXPECTED_BRIEFING_CHECKS = (
    "ck_irene_briefing_status_vocabulary",
    "ck_irene_briefing_skip_reason_vocabulary",
    "ck_irene_briefing_skip_reason_iff_skipped",
    "ck_irene_briefing_texts_iff_written",
)

_EXPECTED_SCOPE_CHECKS = (
    "ck_watch_family_scope_family_vocabulary",
    "ck_watch_family_scope_preset_vocabulary",
    "ck_watch_family_scope_rss_has_no_preset",
)

_BRIEFING_UNIQUE = "uq_irene_briefing_tenant_beat_at"
_SCOPE_UNIQUE = "uq_watch_family_scope_tenant_family_effective_from"


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


async def _numeric_shape(
    engine: AsyncEngine, table: str, column: str
) -> tuple[str, int | None, int | None, str] | None:
    """Return ``(data_type, precision, scale, is_nullable)`` or ``None``."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT data_type, numeric_precision, numeric_scale, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
        ).first()
    if row is None:
        return None
    return row[0], row[1], row[2], row[3]


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


async def _audit_trigger_names(engine: AsyncEngine, table: str) -> set[str]:
    """Return the table's non-internal triggers bound to the audit function."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT tg.tgname
                FROM pg_trigger tg
                JOIN pg_class t ON tg.tgrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                JOIN pg_proc p ON tg.tgfoid = p.oid
                WHERE n.nspname = 'public'
                  AND t.relname = :t
                  AND NOT tg.tgisinternal
                  AND p.proname = 'audit_trigger_function'
                """
            ),
            {"t": table},
        )
        return {row[0] for row in result.fetchall()}


async def _fk_columns(engine: AsyncEngine, table: str) -> set[str]:
    """Return the local column names covered by a FOREIGN KEY on ``table``."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT a.attname
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                JOIN unnest(c.conkey) AS k(attnum) ON TRUE
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                WHERE n.nspname = 'public'
                  AND t.relname = :t
                  AND c.contype = 'f'
                """
            ),
            {"t": table},
        )
        return {row[0] for row in result.fetchall()}


async def _assert_full_shape(engine: AsyncEngine) -> None:
    """Assert both tables and the six WARN columns carry the ADR-0133 shape."""
    briefing_columns = await _columns(engine, _BRIEFING)
    assert briefing_columns == _EXPECTED_BRIEFING_COLUMNS, (
        "irene_briefing column set drifted from the ADR-0133 §1.1 shape: "
        f"missing={sorted(_EXPECTED_BRIEFING_COLUMNS - briefing_columns)}, "
        f"extra={sorted(briefing_columns - _EXPECTED_BRIEFING_COLUMNS)}"
    )
    scope_columns = await _columns(engine, _SCOPE)
    assert scope_columns == _EXPECTED_SCOPE_COLUMNS, (
        "watch_family_scope column set drifted from the ADR-0133 §2.1 shape: "
        f"missing={sorted(_EXPECTED_SCOPE_COLUMNS - scope_columns)}, "
        f"extra={sorted(scope_columns - _EXPECTED_SCOPE_COLUMNS)}"
    )

    # Append-only, as a schema fact: no column that would invite an update.
    for table, columns in ((_BRIEFING, briefing_columns), (_SCOPE, scope_columns)):
        assert "updated_at" not in columns, (
            f"{table} must carry no updated_at: a briefing row is immutable and a "
            "scope change inserts a later revision (ADR-0133 §1.1, §2.1)."
        )

    briefing_checks = await _constraint_names(engine, _BRIEFING, "c")
    for name in _EXPECTED_BRIEFING_CHECKS:
        assert name in briefing_checks, f"irene_briefing is missing the {name} CHECK."
    scope_checks = await _constraint_names(engine, _SCOPE, "c")
    for name in _EXPECTED_SCOPE_CHECKS:
        assert name in scope_checks, f"watch_family_scope is missing the {name} CHECK."

    assert _BRIEFING_UNIQUE in await _constraint_names(engine, _BRIEFING, "u"), (
        f"irene_briefing is missing {_BRIEFING_UNIQUE}; one beat could carry two briefings."
    )
    assert _SCOPE_UNIQUE in await _constraint_names(engine, _SCOPE, "u"), (
        f"watch_family_scope is missing {_SCOPE_UNIQUE}."
    )

    for table in (_BRIEFING, _SCOPE):
        enabled, forced = await _rls_enabled_and_forced(engine, table)
        assert enabled and forced, (
            f"{table} must have RLS enabled AND forced after apply_tenant_rls."
        )
        assert await _has_tenant_isolation_policy(engine, table), (
            f"{table} is missing the standard tenant_isolation policy."
        )

    briefing_triggers = await _audit_trigger_names(engine, _BRIEFING)
    assert briefing_triggers == set(), (
        "irene_briefing must carry no audit trigger: a briefing is a system "
        f"artefact with no human actor (the irene_finding idiom). Found {sorted(briefing_triggers)}."
    )
    scope_triggers = await _audit_trigger_names(engine, _SCOPE)
    assert scope_triggers == {"watch_family_scope_audit_trigger"}, (
        "watch_family_scope must carry exactly the generic audit trigger: a family "
        f"switch is an owner's decision (ADR-0116 §1). Found {sorted(scope_triggers)}."
    )

    assert "changed_by" in await _fk_columns(engine, _SCOPE), (
        "watch_family_scope.changed_by must reference users.id."
    )

    for column in _WARN_COLUMNS:
        shape = await _numeric_shape(engine, _CALIBRATION, column)
        assert shape == ("numeric", 6, 3, "YES"), (
            f"floor_calibration.{column} must be a nullable numeric(6,3) "
            f"(NULL inherits warn_default_pct, ADR-0133 §3.1); found {shape}."
        )
    assert "warn_pct_rss" not in await _columns(engine, _CALIBRATION), (
        "floor_calibration must carry no warn_pct_rss: a press-coverage cluster "
        "has no scalar magnitude (D-WD-13)."
    )


async def test_b036_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    for table in (_BRIEFING, _SCOPE):
        assert await _table_exists(scratch_superuser_engine, table), (
            f"{table} missing before round-trip — is the DB at head?"
        )
    calibration_columns = await _columns(scratch_superuser_engine, _CALIBRATION)
    assert set(_WARN_COLUMNS) <= calibration_columns, (
        "floor_calibration lacks the per-family WARN columns before round-trip."
    )

    try:
        # 1) downgrade to b034: both tables and the six columns are gone.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        for table in (_BRIEFING, _SCOPE):
            assert not await _table_exists(scratch_superuser_engine, table), (
                f"{table} still present after downgrade to {_BELOW}"
            )
        leftover = set(_WARN_COLUMNS) & await _columns(scratch_superuser_engine, _CALIBRATION)
        assert not leftover, f"WARN columns still present after downgrade: {sorted(leftover)}"

        # 2) upgrade head → all of it is back, in full shape.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        for table in (_BRIEFING, _SCOPE):
            assert await _table_exists(scratch_superuser_engine, table), (
                f"{table} missing after re-upgrade to head"
            )

        await _assert_full_shape(scratch_superuser_engine)
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
