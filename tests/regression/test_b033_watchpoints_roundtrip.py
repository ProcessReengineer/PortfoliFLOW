# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b033 (ADR-0116 §1/§7).

Exercises the b033 migration's reversibility: from ``head`` it downgrades
to b032 (dropping ``watchpoints`` and ``floor_calibration``), asserts both
tables are gone, then ``upgrade head`` again and asserts they are back in
the full shape the ADR specifies — the per-family CHECKs that carry the
overlay/defined **asymmetry**, the historisation unique constraints, RLS
enabled + forced with the standard ``tenant_isolation`` policy, and — the
inverse of b032's guard — the generic audit trigger *present* on both.
That contrast is deliberate and worth pinning: b032 omits the trigger
because full row images would copy secrets into ``audit_log``; b033
attaches it because a threshold change is exactly what BAIT/VAIT-grade
explainability must capture (ADR-0116 §1).

One further invariant gets its own assertion: **no ``fund_closure``
column exists anywhere in ``floor_calibration``**. It is a pinned level
(floor = cap = 10, ADR-0116 §7 invariant 1), and giving it nowhere to be
stored is what makes it non-editable. A future migration that "completes"
the column set would silently turn a pinned level into a tenant knob.

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is,
so a relative downgrade silently stops undoing *this* migration the moment
a newer one lands. Naming b033's own ``down_revision`` keeps the guard
honest for every future head.

Runs against a **per-test scratch database** (``tests/regression/conftest.py``)
like the other migration guards, so no downgrade ever touches the shared
dev database. If the server is unreachable the test skips.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

_WATCHPOINTS = "watchpoints"
_CALIBRATION = "floor_calibration"
#: The revision immediately below b033 — i.e. b033's own ``down_revision``.
_BELOW = "b032_add_scoped_settings"

_EXPECTED_WATCHPOINT_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "watchpoint_id",
        "tenant_id",
        "effective_from",
        "retired",
        "family",
        "subject_key",
        "display_name",
        "muted",
        "warn_threshold_pct",
        "re_trigger_delta",
        "instrument_id",
        "currency_pair",
        "drop_pct",
        "move_pct",
        "window_days",
        "max_age_days",
        "horizon_months",
        "min_coverage_ratio",
        "notes",
        "created_at",
        "updated_at",
    }
)

_EXPECTED_CALIBRATION_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "effective_from",
        "warn_default_pct",
        "re_trigger_delta_saa",
        "re_trigger_delta_anlv",
        "re_trigger_delta_rss",
        "re_trigger_delta_price",
        "re_trigger_delta_fx",
        "re_trigger_delta_freshness",
        "re_trigger_delta_liquidity",
        "band_boundary_0",
        "band_boundary_1",
        "options_min_band",
        "floor_limit_breach",
        "floor_limit_escalation",
        "floor_all_clear",
        "floor_rss_cluster",
        "floor_price_trigger",
        "floor_fx_trigger",
        "floor_freshness_trigger",
        "floor_liquidity_trigger",
        "cap_source_internal",
        "cap_source_rss",
        "cap_limit_breach",
        "cap_limit_escalation",
        "cap_all_clear",
        "cap_rss_cluster",
        "cap_price_trigger",
        "cap_fx_trigger",
        "cap_freshness_trigger",
        "cap_liquidity_trigger",
        "notes",
        "created_at",
        "updated_at",
    }
)

_EXPECTED_WATCHPOINT_CHECKS = (
    "ck_watchpoints_family_vocabulary",
    "ck_watchpoints_overlay_family_defines_nothing",
    "ck_watchpoints_rss_carries_mute_only",
    "ck_watchpoints_price_shape",
    "ck_watchpoints_fx_shape",
    "ck_watchpoints_freshness_shape",
    "ck_watchpoints_liquidity_shape",
)

_EXPECTED_CALIBRATION_CHECKS = (
    "ck_floor_calibration_band_boundaries_paired",
    "ck_floor_calibration_options_min_band_vocabulary",
)

_WATCHPOINT_UNIQUE = "uq_watchpoints_tenant_identity_effective_from"
_CALIBRATION_UNIQUE = "uq_floor_calibration_tenant_effective_from"


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


async def _assert_full_shape(engine: AsyncEngine) -> None:
    """Assert both tables carry the ADR-0116 shape."""
    watchpoint_columns = await _columns(engine, _WATCHPOINTS)
    assert watchpoint_columns == _EXPECTED_WATCHPOINT_COLUMNS, (
        "watchpoints column set drifted from the ADR-0116 §1 shape: "
        f"missing={sorted(_EXPECTED_WATCHPOINT_COLUMNS - watchpoint_columns)}, "
        f"extra={sorted(watchpoint_columns - _EXPECTED_WATCHPOINT_COLUMNS)}"
    )

    calibration_columns = await _columns(engine, _CALIBRATION)
    assert calibration_columns == _EXPECTED_CALIBRATION_COLUMNS, (
        "floor_calibration column set drifted from the ADR-0116 §7 shape: "
        f"missing={sorted(_EXPECTED_CALIBRATION_COLUMNS - calibration_columns)}, "
        f"extra={sorted(calibration_columns - _EXPECTED_CALIBRATION_COLUMNS)}"
    )

    # ADR-0116 §7 invariant 1, as a schema fact rather than a convention.
    fund_closure_columns = sorted(c for c in calibration_columns if "fund_closure" in c)
    assert not fund_closure_columns, (
        f"floor_calibration must carry no fund_closure column; found "
        f"{fund_closure_columns}. fund_closure is a pinned level "
        "(floor = cap = 10), and having nowhere to store it is what makes it "
        "non-editable (ADR-0116 §7 invariant 1)."
    )

    watchpoint_checks = await _constraint_names(engine, _WATCHPOINTS, "c")
    for name in _EXPECTED_WATCHPOINT_CHECKS:
        assert name in watchpoint_checks, (
            f"watchpoints is missing the {name} CHECK — the per-family "
            "asymmetry is enforced by the schema, not by the repository."
        )

    calibration_checks = await _constraint_names(engine, _CALIBRATION, "c")
    for name in _EXPECTED_CALIBRATION_CHECKS:
        assert name in calibration_checks, f"floor_calibration is missing the {name} CHECK."

    assert _WATCHPOINT_UNIQUE in await _constraint_names(engine, _WATCHPOINTS, "u"), (
        f"watchpoints is missing {_WATCHPOINT_UNIQUE}; without it one identity "
        "could carry two versions at the same instant."
    )
    assert _CALIBRATION_UNIQUE in await _constraint_names(engine, _CALIBRATION, "u"), (
        f"floor_calibration is missing {_CALIBRATION_UNIQUE}."
    )

    for table in (_WATCHPOINTS, _CALIBRATION):
        enabled, forced = await _rls_enabled_and_forced(engine, table)
        assert enabled and forced, (
            f"{table} must have RLS enabled AND forced after apply_tenant_rls."
        )
        assert await _has_tenant_isolation_policy(engine, table), (
            f"{table} is missing the standard tenant_isolation policy."
        )
        triggers = await _audit_trigger_names(engine, table)
        assert triggers == {f"{table}_audit_trigger"}, (
            f"{table} must carry exactly the generic audit trigger (ADR-0116 §1): "
            "versioning gives reproducibility, the trigger gives actor "
            f"attribution, and threshold changes need both. Found {sorted(triggers)}."
        )


async def test_b033_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    for table in (_WATCHPOINTS, _CALIBRATION):
        assert await _table_exists(scratch_superuser_engine, table), (
            f"{table} missing before round-trip — is the DB at head?"
        )

    try:
        # 1) downgrade to b032: both tables are gone.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        for table in (_WATCHPOINTS, _CALIBRATION):
            assert not await _table_exists(scratch_superuser_engine, table), (
                f"{table} still present after downgrade to {_BELOW}"
            )

        # 2) upgrade head → both are back, in full shape.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        for table in (_WATCHPOINTS, _CALIBRATION):
            assert await _table_exists(scratch_superuser_engine, table), (
                f"{table} missing after re-upgrade to head"
            )

        await _assert_full_shape(scratch_superuser_engine)
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
