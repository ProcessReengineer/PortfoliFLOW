# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The ``watchpoints`` asymmetry, proven at the SQL level (ADR-0116 §1).

Every insert here goes through **raw SQL**, bypassing
:class:`~core.repositories.watchpoint_repository.WatchpointRepository`
entirely. That is the whole point: the repository validates the same
rules and would reject these rows with a friendly message, but the ADR
places the guarantee in the schema — "a UI or repository bug cannot
create a second edit point for limits; the schema forbids it". A test
that went through the repository would prove the repository works, not
that the schema does.

The rules under test, per family:

* ``saa`` / ``anlv`` — sensitivity overlay only: every defining column
  forced NULL. This is the load-bearing one. Subject identity and
  ceilings live with the limit set, and there is never a second place to
  edit them.
* ``rss`` — ``muted`` alone; a cluster subject is non-scalar, so a WARN
  fraction and a magnitude delta have nothing to measure against.
* ``price`` / ``fx`` / ``freshness`` / ``liquidity`` — their own
  parameters required, every other family's forbidden.

Each family gets both directions: a well-formed row is accepted (so a
CHECK that rejected everything would fail the test rather than pass it),
and each forbidden combination is rejected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)

_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

_INSERT = text(
    """
    INSERT INTO watchpoints (
        watchpoint_id, tenant_id, effective_from, retired, family,
        subject_key, display_name, muted, warn_threshold_pct,
        re_trigger_delta, instrument_id, currency_pair, drop_pct,
        move_pct, window_days, max_age_days, horizon_months,
        min_coverage_ratio, notes
    ) VALUES (
        :watchpoint_id, :tenant_id, :effective_from, FALSE, :family,
        :subject_key, :display_name, :muted, :warn_threshold_pct,
        :re_trigger_delta, :instrument_id, :currency_pair, :drop_pct,
        :move_pct, :window_days, :max_age_days, :horizon_months,
        :min_coverage_ratio, NULL
    )
    """
)

_NULL_PARAMETERS: dict[str, object] = {
    "muted": False,
    "warn_threshold_pct": None,
    "re_trigger_delta": None,
    "instrument_id": None,
    "currency_pair": None,
    "drop_pct": None,
    "move_pct": None,
    "window_days": None,
    "max_age_days": None,
    "horizon_months": None,
    "min_coverage_ratio": None,
}


async def _insert(engine: AsyncEngine, tenant_id: UUID, family: str, **parameters: object) -> None:
    """Insert one raw watchpoint row, RLS-scoped like the application."""
    payload = {
        **_NULL_PARAMETERS,
        **parameters,
        "watchpoint_id": uuid4(),
        "tenant_id": tenant_id,
        "effective_from": _NOW,
        "family": family,
        "subject_key": f"{family}:test",
        "display_name": f"{family} test",
    }
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        await conn.execute(_INSERT, payload)


async def _seed_instrument(app_engine: AsyncEngine, tenant_id: UUID) -> UUID:
    """Create the one investment the ``price`` rows point at."""
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(
            email="checks@example.test", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_id, user_id=user.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="equities", display_name="Equities"
        )
        investment = await InvestmentRepository(session).create(
            name="Investment A",
            investment_type="listed_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=user.id,
        )
    return investment.id


# ---------------------------------------------------------------------------
# Positive controls — a well-formed row of each family is accepted.
# ---------------------------------------------------------------------------


async def test_every_family_accepts_its_own_well_formed_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-CHECK-OK")
    instrument_id = await _seed_instrument(app_engine, tenant_id)

    await _insert(app_engine, tenant_id, "saa", muted=True, warn_threshold_pct=85)
    await _insert(app_engine, tenant_id, "anlv", re_trigger_delta=3)
    await _insert(app_engine, tenant_id, "rss", muted=True)
    await _insert(
        app_engine,
        tenant_id,
        "price",
        instrument_id=instrument_id,
        drop_pct=5,
        window_days=5,
    )
    await _insert(app_engine, tenant_id, "fx", currency_pair="USD/EUR", move_pct=3, window_days=5)
    await _insert(app_engine, tenant_id, "freshness", max_age_days=120)
    await _insert(app_engine, tenant_id, "liquidity", horizon_months=12, min_coverage_ratio=1.2)


async def test_family_vocabulary_is_closed(app_engine: AsyncEngine, seed_tenant) -> None:
    """No ``pacing`` family, and no invented ones (ADR-0116 Non-goals)."""
    tenant_id = await seed_tenant("WP-CHECK-VOCAB")
    for family in ("pacing", "PRICE", "nav"):
        with pytest.raises(IntegrityError):
            await _insert(app_engine, tenant_id, family)


# ---------------------------------------------------------------------------
# The asymmetry — saa / anlv define nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["saa", "anlv"])
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("drop_pct", 5),
        ("move_pct", 3),
        ("window_days", 5),
        ("max_age_days", 120),
        ("horizon_months", 12),
        ("min_coverage_ratio", 1.2),
        ("currency_pair", "USD/EUR"),
    ],
)
async def test_overlay_family_cannot_define_a_parameter(
    app_engine: AsyncEngine, seed_tenant, family: str, column: str, value: object
) -> None:
    """An overlay row that defines anything is refused by the schema.

    This is the constraint that makes "there is never a second edit point
    for limits" a structural fact rather than a coding convention.
    """
    tenant_id = await seed_tenant(f"WP-{family}-{column}"[:40])
    with pytest.raises(IntegrityError):
        await _insert(app_engine, tenant_id, family, **{column: value})


@pytest.mark.parametrize("family", ["saa", "anlv"])
async def test_overlay_family_cannot_name_an_instrument(
    app_engine: AsyncEngine, seed_tenant, family: str
) -> None:
    tenant_id = await seed_tenant(f"WP-{family}-instrument")
    instrument_id = await _seed_instrument(app_engine, tenant_id)
    with pytest.raises(IntegrityError):
        await _insert(app_engine, tenant_id, family, instrument_id=instrument_id)


# ---------------------------------------------------------------------------
# rss — mute only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [("warn_threshold_pct", 85), ("re_trigger_delta", 5), ("window_days", 5)],
)
async def test_rss_carries_mute_only(
    app_engine: AsyncEngine, seed_tenant, column: str, value: object
) -> None:
    tenant_id = await seed_tenant(f"WP-rss-{column}"[:40])
    with pytest.raises(IntegrityError):
        await _insert(app_engine, tenant_id, "rss", **{column: value})


# ---------------------------------------------------------------------------
# price — instrument_id + drop_pct + window_days, nothing else.
# ---------------------------------------------------------------------------


async def test_price_requires_its_full_parameter_set(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("WP-price-required")
    instrument_id = await _seed_instrument(app_engine, tenant_id)
    complete = {"instrument_id": instrument_id, "drop_pct": 5, "window_days": 5}
    for missing in complete:
        partial = {key: value for key, value in complete.items() if key != missing}
        with pytest.raises(IntegrityError):
            await _insert(app_engine, tenant_id, "price", **partial)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("currency_pair", "USD/EUR"),
        ("move_pct", 3),
        ("max_age_days", 120),
        ("horizon_months", 12),
        ("min_coverage_ratio", 1.2),
    ],
)
async def test_price_cannot_borrow_another_familys_parameter(
    app_engine: AsyncEngine, seed_tenant, column: str, value: object
) -> None:
    tenant_id = await seed_tenant(f"WP-price-{column}"[:40])
    instrument_id = await _seed_instrument(app_engine, tenant_id)
    with pytest.raises(IntegrityError):
        await _insert(
            app_engine,
            tenant_id,
            "price",
            instrument_id=instrument_id,
            drop_pct=5,
            window_days=5,
            **{column: value},
        )


# ---------------------------------------------------------------------------
# fx — currency_pair + move_pct + window_days, nothing else.
# ---------------------------------------------------------------------------


async def test_fx_requires_its_full_parameter_set(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("WP-fx-required")
    complete = {"currency_pair": "USD/EUR", "move_pct": 3, "window_days": 5}
    for missing in complete:
        partial = {key: value for key, value in complete.items() if key != missing}
        with pytest.raises(IntegrityError):
            await _insert(app_engine, tenant_id, "fx", **partial)


@pytest.mark.parametrize(
    ("column", "value"),
    [("drop_pct", 5), ("max_age_days", 120), ("horizon_months", 12), ("min_coverage_ratio", 1.2)],
)
async def test_fx_cannot_borrow_another_familys_parameter(
    app_engine: AsyncEngine, seed_tenant, column: str, value: object
) -> None:
    tenant_id = await seed_tenant(f"WP-fx-{column}"[:40])
    with pytest.raises(IntegrityError):
        await _insert(
            app_engine,
            tenant_id,
            "fx",
            currency_pair="USD/EUR",
            move_pct=3,
            window_days=5,
            **{column: value},
        )


async def test_fx_cannot_name_an_instrument(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("WP-fx-instrument")
    instrument_id = await _seed_instrument(app_engine, tenant_id)
    with pytest.raises(IntegrityError):
        await _insert(
            app_engine,
            tenant_id,
            "fx",
            currency_pair="USD/EUR",
            move_pct=3,
            window_days=5,
            instrument_id=instrument_id,
        )


# ---------------------------------------------------------------------------
# freshness / liquidity — the singleton families.
# ---------------------------------------------------------------------------


async def test_freshness_requires_max_age_and_nothing_else(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-freshness")
    with pytest.raises(IntegrityError):
        await _insert(app_engine, tenant_id, "freshness")
    for column, value in (
        ("window_days", 5),
        ("drop_pct", 5),
        ("horizon_months", 12),
        ("min_coverage_ratio", 1.2),
        ("currency_pair", "USD/EUR"),
    ):
        with pytest.raises(IntegrityError):
            await _insert(app_engine, tenant_id, "freshness", max_age_days=120, **{column: value})


async def test_liquidity_requires_both_parameters_and_nothing_else(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-liquidity")
    complete = {"horizon_months": 12, "min_coverage_ratio": 1.2}
    for missing in complete:
        partial = {key: value for key, value in complete.items() if key != missing}
        with pytest.raises(IntegrityError):
            await _insert(app_engine, tenant_id, "liquidity", **partial)
    for column, value in (("max_age_days", 120), ("window_days", 5), ("move_pct", 3)):
        with pytest.raises(IntegrityError):
            await _insert(app_engine, tenant_id, "liquidity", **complete, **{column: value})


# ---------------------------------------------------------------------------
# Historisation and floor_calibration shape.
# ---------------------------------------------------------------------------


async def test_one_identity_cannot_have_two_versions_at_one_instant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The historisation unique constraint, at the SQL level."""
    tenant_id = await seed_tenant("WP-unique")
    identity = uuid4()
    payload = {
        **_NULL_PARAMETERS,
        "watchpoint_id": identity,
        "tenant_id": tenant_id,
        "effective_from": _NOW,
        "family": "freshness",
        "subject_key": "freshness:*",
        "display_name": "NAV freshness",
        "max_age_days": 120,
    }
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        await conn.execute(_INSERT, payload)
    with pytest.raises(IntegrityError):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            await conn.execute(_INSERT, payload)


async def test_floor_calibration_band_boundaries_are_set_together_or_not_at_all(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("FC-boundaries")
    statement = text(
        "INSERT INTO floor_calibration (tenant_id, effective_from, "
        "band_boundary_0, band_boundary_1) "
        "VALUES (:tid, :eff, :b0, :b1)"
    )
    for b0, b1 in ((3, None), (None, 6)):
        with pytest.raises(IntegrityError):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(tenant_id)},
                )
                await conn.execute(statement, {"tid": tenant_id, "eff": _NOW, "b0": b0, "b1": b1})


async def test_floor_calibration_options_band_vocabulary_is_closed(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("FC-options-band")
    with pytest.raises(IntegrityError):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            await conn.execute(
                text(
                    "INSERT INTO floor_calibration (tenant_id, effective_from, "
                    "options_min_band) VALUES (:tid, :eff, 'urgent')"
                ),
                {"tid": tenant_id, "eff": _NOW},
            )
