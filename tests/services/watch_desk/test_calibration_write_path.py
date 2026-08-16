# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The floor-calibration write path, against the live compose Postgres.

Covers the seam ADR-0116 §5/§7 defines: a revision is validated *as a
whole configuration* before it is written, and what lands in the table
is only the tenant's deviations.

* FC-01: deviations only — a revision that restates a default stores
  NULL, so a future change to that default still reaches the tenant.
* FC-02: historisation — ``effective_calibration`` resolves by instant,
  and a revision cannot back-date.
* FC-03: the pinned invariants are enforced at **write** time, including
  the ``limit_breach`` floor / band-boundary coupling. The beat must
  never be the first to discover an inverted configuration.
* FC-04: ``fund_closure`` is refused with its own reason, not as an
  unknown key — it is pinned, not missing.
* FC-05: ``effective_floor_config`` composes the stored revision for the
  beat, and ``effective_warn_threshold_pct`` resolves the WARN default
  that is deliberately not part of ``FloorConfig``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import FloorCalibrationInvalid
from core.repositories import FloorCalibrationRepository, tenant_context
from services.analytics.irene_floor import (
    DEFAULT_FLOOR_CONFIG,
    DEFAULT_WARN_THRESHOLD_PCT,
    SOURCE_RSS,
    TRIGGER_ALL_CLEAR,
    TRIGGER_LIMIT_BREACH,
    TRIGGER_PRICE,
)
from services.watch_desk.calibration import (
    effective_floor_config,
    effective_warn_threshold_pct,
    save_calibration_revision,
)

_T0 = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=30)


# ---------------------------------------------------------------------------
# FC-01: deviations only
# ---------------------------------------------------------------------------


async def test_fc01_a_restated_default_is_stored_as_null(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("FC-01")

    async with tenant_context(app_engine, tenant_id) as session:
        saved = await save_calibration_revision(
            FloorCalibrationRepository(session),
            effective_from=_T0,
            # Everything here equals a code default except the price floor.
            warn_default_pct=DEFAULT_WARN_THRESHOLD_PCT,
            band_boundaries=DEFAULT_FLOOR_CONFIG.band_boundaries,
            options_min_band=DEFAULT_FLOOR_CONFIG.options_min_band,
            floor={
                TRIGGER_PRICE: 6,
                TRIGGER_LIMIT_BREACH: DEFAULT_FLOOR_CONFIG.floor[TRIGGER_LIMIT_BREACH],
            },
            re_trigger_delta={"saa": DEFAULT_FLOOR_CONFIG.re_trigger_delta["saa"]},
        )

    assert dict(saved.floor) == {TRIGGER_PRICE: 6}, (
        "only the deviating floor is stored; the restated default is NULL"
    )
    assert dict(saved.re_trigger_delta) == {}
    assert saved.warn_default_pct is None
    assert saved.band_boundaries is None
    assert saved.options_min_band is None

    # And the columns really are NULL underneath, not merely absent from
    # the DTO's sparse view.
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        row = (
            await conn.execute(
                text(
                    "SELECT floor_price_trigger, floor_limit_breach, warn_default_pct, "
                    "band_boundary_0, options_min_band FROM floor_calibration"
                )
            )
        ).one()
    assert row.floor_price_trigger == 6
    assert row.floor_limit_breach is None
    assert row.warn_default_pct is None
    assert row.band_boundary_0 is None
    assert row.options_min_band is None


async def test_fc01_numeric_equality_not_string_equality_decides_deviation(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``90`` and ``90.0`` are both the default, and neither is stored."""
    tenant_id = await seed_tenant("FC-01b")
    async with tenant_context(app_engine, tenant_id) as session:
        saved = await save_calibration_revision(
            FloorCalibrationRepository(session),
            effective_from=_T0,
            warn_default_pct=Decimal("90.000"),
        )
    assert saved.warn_default_pct is None


# ---------------------------------------------------------------------------
# FC-02: historisation
# ---------------------------------------------------------------------------


async def test_fc02_effective_calibration_resolves_by_instant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("FC-02")

    async with tenant_context(app_engine, tenant_id) as session:
        repository = FloorCalibrationRepository(session)
        await save_calibration_revision(repository, effective_from=_T0, floor={TRIGGER_PRICE: 5})
        await save_calibration_revision(repository, effective_from=_T1, floor={TRIGGER_PRICE: 7})

    async with tenant_context(app_engine, tenant_id) as session:
        repository = FloorCalibrationRepository(session)
        assert await repository.effective_calibration(_T0 - timedelta(days=1)) is None
        at_t0 = await repository.effective_calibration(_T0)
        at_t1 = await repository.effective_calibration(_T1)
        assert at_t0 is not None and at_t0.floor[TRIGGER_PRICE] == 5
        assert at_t1 is not None and at_t1.floor[TRIGGER_PRICE] == 7
        assert len(await repository.list_revisions()) == 2


async def test_fc02_a_revision_cannot_backdate(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("FC-02b")
    async with tenant_context(app_engine, tenant_id) as session:
        repository = FloorCalibrationRepository(session)
        await save_calibration_revision(repository, effective_from=_T1)
        with pytest.raises(FloorCalibrationInvalid) as excinfo:
            await save_calibration_revision(repository, effective_from=_T0)
        assert excinfo.value.field == "effective_from"


# ---------------------------------------------------------------------------
# FC-03: the pinned invariants, at write time
# ---------------------------------------------------------------------------


async def test_fc03_a_boundary_edit_that_strands_the_breach_floor_is_refused(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The coupling test the acceptance criteria call for.

    Raising the upper band boundary to 8 puts the critical band at 9–10,
    which strands the default ``limit_breach`` floor of 7 below it. The
    write path refuses before anything is persisted.
    """
    tenant_id = await seed_tenant("FC-03")
    async with tenant_context(app_engine, tenant_id) as session:
        repository = FloorCalibrationRepository(session)
        with pytest.raises(FloorCalibrationInvalid, match="below the critical band"):
            await save_calibration_revision(repository, effective_from=_T0, band_boundaries=(3, 8))
        assert await repository.list_revisions() == [], "nothing may be persisted"

        # The same edit with the floor moved along is accepted.
        saved = await save_calibration_revision(
            repository,
            effective_from=_T0,
            band_boundaries=(3, 8),
            floor={TRIGGER_LIMIT_BREACH: 9},
        )
        assert saved.band_boundaries == (3, 8)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"cap": {SOURCE_RSS: 5}}, "never outranks an internal finding"),
        ({"cap": {TRIGGER_ALL_CLEAR: 7}}, "never itself urgent"),
        ({"band_boundaries": (2, 6)}, "never outranks an internal finding"),
        ({"floor": {TRIGGER_PRICE: 11}}, "clamp would invert"),
        ({"band_boundaries": (6, 3)}, "strictly monotonic"),
    ],
)
async def test_fc03_invalid_configurations_are_refused_at_write_time(
    app_engine: AsyncEngine, seed_tenant, overrides: dict, message: str
) -> None:
    tenant_id = await seed_tenant(f"FC-03-{message[:12]}")
    async with tenant_context(app_engine, tenant_id) as session:
        repository = FloorCalibrationRepository(session)
        with pytest.raises(FloorCalibrationInvalid, match=message):
            await save_calibration_revision(repository, effective_from=_T0, **overrides)
        assert await repository.list_revisions() == []


@pytest.mark.parametrize("warn", [Decimal("50"), Decimal("100"), Decimal("101")])
async def test_fc03_warn_default_must_lie_strictly_inside_50_to_100(
    app_engine: AsyncEngine, seed_tenant, warn: Decimal
) -> None:
    tenant_id = await seed_tenant(f"FC-03w-{warn}")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(FloorCalibrationInvalid) as excinfo:
            await save_calibration_revision(
                FloorCalibrationRepository(session),
                effective_from=_T0,
                warn_default_pct=warn,
            )
        assert excinfo.value.field == "warn_default_pct"


# ---------------------------------------------------------------------------
# FC-04: fund_closure is pinned, not merely unknown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group", ["floor", "cap"])
async def test_fc04_fund_closure_is_refused_with_its_own_reason(
    app_engine: AsyncEngine, seed_tenant, group: str
) -> None:
    tenant_id = await seed_tenant(f"FC-04-{group}")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(FloorCalibrationInvalid, match="pinned level"):
            await save_calibration_revision(
                FloorCalibrationRepository(session),
                effective_from=_T0,
                **{group: {"fund_closure": 8}},
            )


async def test_fc04_an_unknown_key_is_refused_as_unknown(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("FC-04b")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(FloorCalibrationInvalid, match="Unknown floor key"):
            await save_calibration_revision(
                FloorCalibrationRepository(session),
                effective_from=_T0,
                floor={"pacing_trigger": 5},
            )


# ---------------------------------------------------------------------------
# FC-05: what the beat reads back
# ---------------------------------------------------------------------------


async def test_fc05_effective_floor_config_composes_the_stored_revision(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("FC-05")

    async with tenant_context(app_engine, tenant_id) as session:
        repository = FloorCalibrationRepository(session)
        # No revision yet: the beat gets the code defaults.
        assert await effective_floor_config(repository, _T0) is DEFAULT_FLOOR_CONFIG

        await save_calibration_revision(
            repository,
            effective_from=_T0,
            floor={TRIGGER_PRICE: 6},
            re_trigger_delta={"fx": Decimal("2.0")},
        )

        composed = await effective_floor_config(repository, _T1)
        assert composed.floor[TRIGGER_PRICE] == 6
        assert composed.re_trigger_delta["fx"] == Decimal("2.0000")
        assert composed.re_trigger_delta["saa"] == DEFAULT_FLOOR_CONFIG.re_trigger_delta["saa"]
        assert composed.cap == DEFAULT_FLOOR_CONFIG.cap


async def test_fc05_warn_threshold_resolves_from_the_row_or_the_default(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("FC-05b")

    assert effective_warn_threshold_pct(None) == DEFAULT_WARN_THRESHOLD_PCT

    async with tenant_context(app_engine, tenant_id) as session:
        repository = FloorCalibrationRepository(session)
        await save_calibration_revision(
            repository, effective_from=_T0, warn_default_pct=Decimal("85")
        )
        calibration = await repository.effective_calibration(_T1)

    assert effective_warn_threshold_pct(calibration) == Decimal("85.000")


async def test_fc05_calibration_is_invisible_across_tenants(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant("FC-05-A")
    tenant_b = await seed_tenant("FC-05-B")

    async with tenant_context(app_engine, tenant_a) as session:
        await save_calibration_revision(
            FloorCalibrationRepository(session), effective_from=_T0, floor={TRIGGER_PRICE: 6}
        )

    async with tenant_context(app_engine, tenant_b) as session:
        repository = FloorCalibrationRepository(session)
        assert await repository.effective_calibration(_T1) is None
        assert await effective_floor_config(repository, _T1) is DEFAULT_FLOOR_CONFIG
