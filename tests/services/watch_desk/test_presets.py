# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Sensitivity presets — values, display and the atomic apply (ADR-0133 §3.2, §3.3).

The first four tests are pure: resolutions are built by hand. The last
five run against the live compose Postgres, through the sanctioned write
path and the scope repository.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import WatchFamilyScopeInvalid
from core.models.watch_family_scope import SCOPE_PRESETS, WATCHPOINT_FAMILIES
from core.repositories import (
    FloorCalibrationRepository,
    WatchFamilyScopeRepository,
    tenant_context,
)
from services.analytics.irene_floor import (  # noqa: PLC2701 — Standard is pinned by identity
    _DEFAULT_RE_TRIGGER_DELTA,
    DEFAULT_FLOOR_CONFIG,
    DEFAULT_WARN_THRESHOLD_PCT,
    TRIGGER_LIMIT_BREACH,
    TRIGGER_PRICE,
)
from services.watch_desk.calibration import save_calibration_revision
from services.watch_desk.overlay import SubjectOverlay, WatchDeskResolution, resolve_watch_desk
from services.watch_desk.presets import (
    CUSTOM,
    PRESET_NERVOUS,
    PRESET_QUIET,
    PRESET_STANDARD,
    SCALAR_FAMILIES,
    PresetValues,
    apply_presets,
    displayed_preset,
    preset_values,
)
from tests.services.irene._book_fixtures import seed_user

_T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=7)


def _resolution(
    *,
    warn_default_pct: Decimal = DEFAULT_WARN_THRESHOLD_PCT,
    family_warn_pct: dict[str, Decimal] | None = None,
    deltas: dict[str, Decimal] | None = None,
    overlays: dict[str, SubjectOverlay] | None = None,
) -> WatchDeskResolution:
    config = dataclasses.replace(
        DEFAULT_FLOOR_CONFIG,
        re_trigger_delta={**DEFAULT_FLOOR_CONFIG.re_trigger_delta, **(deltas or {})},
    )
    return WatchDeskResolution(
        config=config,
        warn_default_pct=warn_default_pct,
        overlays=overlays or {},
        family_warn_pct=family_warn_pct or {},
    )


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def test_the_mapping_matches_adr_0133() -> None:
    assert (PRESET_QUIET, PRESET_STANDARD, PRESET_NERVOUS) == SCOPE_PRESETS
    for family in ("saa", "anlv", "price", "fx", "freshness", "liquidity"):
        assert preset_values("quiet", family) == PresetValues(Decimal("95"), Decimal("8"))
        assert preset_values("standard", family) == PresetValues(Decimal("90"), Decimal("5"))
        assert preset_values("nervous", family) == PresetValues(Decimal("80"), Decimal("3"))

    for preset, family, field in (
        ("quiet", "rss", "family"),
        ("quiet", "pacing", "family"),
        ("custom", "saa", "preset"),
    ):
        with pytest.raises(WatchFamilyScopeInvalid) as raised:
            preset_values(preset, family)
        assert raised.value.field == field, (preset, family)


def test_standard_is_defined_by_reference_to_the_code_defaults() -> None:
    for family in SCALAR_FAMILIES:
        standard = preset_values("standard", family)
        assert standard.warn_pct is DEFAULT_WARN_THRESHOLD_PCT
        assert standard.re_trigger_delta is _DEFAULT_RE_TRIGGER_DELTA[family]
    expected = tuple(family for family in WATCHPOINT_FAMILIES if family != "rss")
    assert expected == SCALAR_FAMILIES


def test_display_reads_the_effective_family_values() -> None:
    defaults = _resolution()
    for family in SCALAR_FAMILIES:
        assert displayed_preset(defaults, family) == "standard", family
    assert displayed_preset(defaults, "rss") is None

    nervous = _resolution(family_warn_pct={"saa": Decimal("80")}, deltas={"saa": Decimal("3")})
    assert displayed_preset(nervous, "saa") == "nervous"
    assert displayed_preset(nervous, "anlv") == "standard"

    custom = _resolution(family_warn_pct={"saa": Decimal("80")})
    assert displayed_preset(custom, "saa") == CUSTOM

    # The tenant default reaches the display through the family step.
    quiet = _resolution(warn_default_pct=Decimal("95"), deltas={"anlv": Decimal("8")})
    assert displayed_preset(quiet, "anlv") == "quiet"

    with pytest.raises(WatchFamilyScopeInvalid) as raised:
        displayed_preset(defaults, "pacing")
    assert raised.value.field == "family"


def test_a_per_subject_override_leaves_the_family_preset() -> None:
    overlay = SubjectOverlay(
        watchpoint_id=uuid4(),
        subject_key="saa:equities",
        family="saa",
        display_name="Equities",
        muted=False,
        warn_threshold_pct=Decimal("70"),
        re_trigger_delta=Decimal("1"),
        notes=None,
    )
    resolution = _resolution(overlays={"saa:equities": overlay})

    # The override is live for its subject …
    assert resolution.warn_threshold_for("saa:equities") == Decimal("70")
    assert resolution.re_trigger_delta_for("saa:equities") == Decimal("1")
    # … and the family still shows its own preset.
    assert displayed_preset(resolution, "saa") == "standard"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


async def _tenant_and_actor(
    superuser_engine: AsyncEngine, seed_tenant, name: str
) -> tuple[UUID, UUID]:
    tenant_id = await seed_tenant(name)
    return tenant_id, await seed_user(superuser_engine, tenant_id)


async def test_apply_round_trips_under_a_tenant_default_that_differs(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, actor = await _tenant_and_actor(superuser_engine, seed_tenant, "PR-05")
    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        await save_calibration_revision(
            FloorCalibrationRepository(session),
            effective_from=_T0,
            warn_default_pct=Decimal("85"),
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        application = await apply_presets(
            session,
            presets={"saa": "standard", "anlv": "nervous", "price": "quiet"},
            actor=actor,
            effective_from=_T1,
        )

    assert dict(application.calibration.warn_pct) == {
        "saa": Decimal("90"),
        "anlv": Decimal("80"),
        "price": Decimal("95"),
    }
    assert dict(application.calibration.re_trigger_delta) == {
        "anlv": Decimal("3"),
        "price": Decimal("8"),
    }

    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        resolution = await resolve_watch_desk(session, as_of=_T1)
    assert displayed_preset(resolution, "saa") == "standard"
    assert displayed_preset(resolution, "anlv") == "nervous"
    assert displayed_preset(resolution, "price") == "quiet"
    assert displayed_preset(resolution, "fx") == CUSTOM, "fx inherits the tenant's 85"
    assert displayed_preset(resolution, "rss") is None


async def test_apply_carries_the_rest_of_the_calibration_forward(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, actor = await _tenant_and_actor(superuser_engine, seed_tenant, "PR-06")
    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        await save_calibration_revision(
            FloorCalibrationRepository(session),
            effective_from=_T0,
            band_boundaries=(3, 8),
            floor={TRIGGER_LIMIT_BREACH: 9},
            cap={TRIGGER_PRICE: 8},
            options_min_band="critical",
            warn_pct={"liquidity": Decimal("75")},
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        application = await apply_presets(
            session, presets={"saa": "nervous"}, actor=actor, effective_from=_T1
        )

    calibration = application.calibration
    assert calibration.effective_from == _T1
    assert calibration.band_boundaries == (3, 8)
    assert dict(calibration.floor) == {TRIGGER_LIMIT_BREACH: 9}
    assert dict(calibration.cap) == {TRIGGER_PRICE: 8}
    assert calibration.options_min_band == "critical"
    assert dict(calibration.warn_pct) == {"liquidity": Decimal("75"), "saa": Decimal("80")}
    assert dict(calibration.re_trigger_delta) == {"saa": Decimal("3")}
    assert calibration.notes is None


async def test_apply_records_one_scope_revision_per_family(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, actor = await _tenant_and_actor(superuser_engine, seed_tenant, "PR-07")
    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        await WatchFamilyScopeRepository(session).insert_revision(
            family="anlv", effective_from=_T0, enabled=False, changed_by=actor
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        application = await apply_presets(
            session, presets={"fx": "nervous", "anlv": "quiet"}, actor=actor, effective_from=_T1
        )

    assert [row.family for row in application.scope] == ["anlv", "fx"]

    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        stored = await WatchFamilyScopeRepository(session).list_revisions()
    applied = [row for row in stored if row.effective_from == _T1]
    assert [row.family for row in applied] == ["anlv", "fx"]
    anlv, fx = applied
    assert anlv.changed_by == actor and fx.changed_by == actor
    assert (anlv.preset, fx.preset) == ("quiet", "nervous")
    assert anlv.enabled is False, "the switch is carried, not reset"
    assert fx.enabled is True


async def test_apply_is_atomic(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, actor = await _tenant_and_actor(superuser_engine, seed_tenant, "PR-08")
    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        await WatchFamilyScopeRepository(session).insert_revision(
            family="anlv", effective_from=_T0, enabled=True, changed_by=actor
        )

    # `saa` is written first; `anlv` at the same instant is then refused.
    # The refusal is caught inside the context, which therefore commits.
    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        with pytest.raises(WatchFamilyScopeInvalid):
            await apply_presets(
                session,
                presets={"saa": "nervous", "anlv": "quiet"},
                actor=actor,
                effective_from=_T0,
            )

    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        calibrations = await FloorCalibrationRepository(session).list_revisions()
        saa_scope = await WatchFamilyScopeRepository(session).list_revisions(family="saa")
    assert [row for row in calibrations if row.effective_from == _T0] == []
    assert saa_scope == []


async def test_apply_refuses_press_coverage_custom_unknown_and_empty(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, actor = await _tenant_and_actor(superuser_engine, seed_tenant, "PR-09")
    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        for presets in ({"rss": "quiet"}, {"saa": "custom"}, {"pacing": "quiet"}, {}):
            with pytest.raises(WatchFamilyScopeInvalid):
                await apply_presets(session, presets=presets, actor=actor, effective_from=_T0)

    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        assert await FloorCalibrationRepository(session).list_revisions() == []
        assert await WatchFamilyScopeRepository(session).list_revisions() == []
