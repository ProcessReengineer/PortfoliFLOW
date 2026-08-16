# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""WatchpointRepository tests against the live compose Postgres.

The schema owns the asymmetry (see
``test_watchpoint_schema_checks.py``); this module covers what the
repository owns (ADR-0116 §1/§3):

* WP-01: historisation — ``create`` then ``revise`` leaves two immutable
  versions, and ``effective_watchpoints`` resolves the right one for a
  given instant.
* WP-02: ``revise`` inherits the identity-defining fields and refuses to
  back-date.
* WP-03: ``retire`` writes a retiring version; the identity leaves the
  shared read but keeps its history.
* WP-04: value bounds — WARN window, positivity, currency-pair format.
* WP-05: the repository mirrors the family shape rules with a *named*
  field, so a caller sees the offending column rather than an
  ``IntegrityError``.
* WP-06: the singleton rule for ``freshness`` and ``liquidity`` —
  versions of one identity fine, a second identity refused, and a fresh
  one permitted again once the first is retired.
* WP-07: RLS isolation — one tenant's watchpoints are invisible to
  another.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import WatchpointInvalid, WatchpointNotFound
from core.repositories import WatchpointRepository, tenant_context

_T0 = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=7)
_T2 = _T1 + timedelta(days=7)


async def _create_freshness(session, *, effective_from=_T0, max_age_days=120):
    return await WatchpointRepository(session).create(
        family="freshness",
        subject_key="freshness:*",
        display_name="NAV freshness (all investments)",
        effective_from=effective_from,
        max_age_days=max_age_days,
    )


# ---------------------------------------------------------------------------
# WP-01 / WP-02: historisation
# ---------------------------------------------------------------------------


async def test_wp01_revise_adds_a_version_and_resolution_follows_the_instant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-01")

    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)
        created = await _create_freshness(session)
        await repository.revise(
            created.watchpoint_id,
            effective_from=_T1,
            display_name="NAV freshness (tightened)",
            max_age_days=60,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)

        # Before the first version there is nothing effective at all.
        assert await repository.effective_watchpoints(_T0 - timedelta(days=1)) == []

        at_t0 = await repository.effective_watchpoints(_T0)
        assert [row.max_age_days for row in at_t0] == [120]

        at_t1 = await repository.effective_watchpoints(_T1)
        assert [row.max_age_days for row in at_t1] == [60]
        assert at_t1[0].watchpoint_id == created.watchpoint_id, (
            "a revision keeps the identity; only the version row is new"
        )

        versions = await repository.list_versions(created.watchpoint_id)
        assert [version.max_age_days for version in versions] == [120, 60]
        assert versions[0].id != versions[1].id, "versions are separate immutable rows"


async def test_wp02_revise_inherits_identity_fields_and_refuses_to_backdate(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-02")

    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)
        created = await repository.create(
            family="fx",
            subject_key="fx:USD/EUR",
            display_name="FX move USD/EUR",
            effective_from=_T0,
            currency_pair="USD/EUR",
            move_pct=Decimal("3.0"),
            window_days=5,
        )
        revised = await repository.revise(
            created.watchpoint_id,
            effective_from=_T1,
            display_name="FX move USD/EUR (tightened)",
            move_pct=Decimal("2.0"),
            window_days=5,
        )

        # Identity-defining fields are carried, not re-supplied.
        assert revised.family == "fx"
        assert revised.subject_key == "fx:USD/EUR"
        assert revised.currency_pair == "USD/EUR"
        assert revised.move_pct == Decimal("2.0")

        with pytest.raises(WatchpointInvalid) as excinfo:
            await repository.revise(
                created.watchpoint_id,
                effective_from=_T0,
                display_name="back-dated",
                move_pct=Decimal("1.0"),
                window_days=5,
            )
        assert excinfo.value.field == "effective_from"


async def test_wp02_revising_an_unknown_identity_raises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    from uuid import uuid4

    tenant_id = await seed_tenant("WP-02b")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchpointNotFound):
            await WatchpointRepository(session).revise(
                uuid4(),
                effective_from=_T1,
                display_name="nothing to revise",
                max_age_days=90,
            )


# ---------------------------------------------------------------------------
# WP-03: retirement
# ---------------------------------------------------------------------------


async def test_wp03_retire_removes_from_the_shared_read_but_keeps_history(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-03")

    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)
        created = await _create_freshness(session)
        retiring = await repository.retire(created.watchpoint_id, effective_from=_T1)

        # The retiring version copies the calibration verbatim — both because
        # the CHECKs demand a well-formed row and because "what was it set to
        # when it was retired" should not need a join.
        assert retiring.retired is True
        assert retiring.max_age_days == 120

        assert await repository.effective_watchpoints(_T1) == []
        assert [row.max_age_days for row in await repository.effective_watchpoints(_T0)] == [120]

        with_retired = await repository.effective_watchpoints(_T1, include_retired=True)
        assert [row.retired for row in with_retired] == [True]
        assert len(await repository.list_versions(created.watchpoint_id)) == 2

        with pytest.raises(WatchpointInvalid):
            await repository.retire(created.watchpoint_id, effective_from=_T2)


# ---------------------------------------------------------------------------
# WP-04: value bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("warn", [Decimal("50"), Decimal("49.9"), Decimal("100"), Decimal("120")])
async def test_wp04_warn_threshold_must_lie_strictly_inside_50_to_100(
    app_engine: AsyncEngine, seed_tenant, warn: Decimal
) -> None:
    tenant_id = await seed_tenant(f"WP-04-{warn}")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchpointInvalid) as excinfo:
            await WatchpointRepository(session).create(
                family="saa",
                subject_key="saa:equities",
                display_name="SAA equities",
                effective_from=_T0,
                warn_threshold_pct=warn,
            )
        assert excinfo.value.field == "warn_threshold_pct"


async def test_wp04_accepts_a_warn_threshold_inside_the_window(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-04-ok")
    async with tenant_context(app_engine, tenant_id) as session:
        created = await WatchpointRepository(session).create(
            family="saa",
            subject_key="saa:equities",
            display_name="SAA equities",
            effective_from=_T0,
            warn_threshold_pct=Decimal("85"),
            re_trigger_delta=Decimal("2.5"),
            muted=True,
        )
    assert created.warn_threshold_pct == Decimal("85.000")
    assert created.muted is True


@pytest.mark.parametrize(
    ("column", "value"),
    [("max_age_days", 0), ("max_age_days", -1), ("re_trigger_delta", Decimal("0"))],
)
async def test_wp04_thresholds_must_be_positive(
    app_engine: AsyncEngine, seed_tenant, column: str, value: object
) -> None:
    tenant_id = await seed_tenant(f"WP-04p-{column}-{value}")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchpointInvalid) as excinfo:
            await WatchpointRepository(session).create(
                family="freshness",
                subject_key="freshness:*",
                display_name="NAV freshness",
                effective_from=_T0,
                **{"max_age_days": 120, column: value},
            )
        assert excinfo.value.field == column


@pytest.mark.parametrize("pair", ["USDEUR", "usd/eur", "USD/EURO", "USD/USD", "USD /EUR"])
async def test_wp04_currency_pair_must_be_a_well_formed_distinct_pair(
    app_engine: AsyncEngine, seed_tenant, pair: str
) -> None:
    tenant_id = await seed_tenant(f"WP-04c-{pair}"[:40])
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchpointInvalid) as excinfo:
            await WatchpointRepository(session).create(
                family="fx",
                subject_key=f"fx:{pair}",
                display_name="FX move",
                effective_from=_T0,
                currency_pair=pair,
                move_pct=Decimal("3.0"),
                window_days=5,
            )
        assert excinfo.value.field == "currency_pair"


async def test_wp04_naive_instants_are_refused(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("WP-04n")
    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)
        with pytest.raises(WatchpointInvalid):
            await repository.create(
                family="freshness",
                subject_key="freshness:*",
                display_name="NAV freshness",
                effective_from=datetime(2026, 8, 1, 9, 0),
                max_age_days=120,
            )
        with pytest.raises(WatchpointInvalid):
            await repository.effective_watchpoints(datetime(2026, 8, 1, 9, 0))


# ---------------------------------------------------------------------------
# WP-05: shape rules, mirrored with a named field
# ---------------------------------------------------------------------------


async def test_wp05_overlay_family_defining_a_parameter_names_the_column(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-05")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchpointInvalid) as excinfo:
            await WatchpointRepository(session).create(
                family="saa",
                subject_key="saa:equities",
                display_name="SAA equities",
                effective_from=_T0,
                drop_pct=Decimal("5.0"),
            )
        assert excinfo.value.field == "drop_pct"


async def test_wp05_missing_required_parameter_names_the_column(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-05b")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchpointInvalid) as excinfo:
            await WatchpointRepository(session).create(
                family="liquidity",
                subject_key="liquidity:cash_coverage",
                display_name="Cash coverage",
                effective_from=_T0,
                horizon_months=12,
            )
        assert excinfo.value.field == "min_coverage_ratio"


async def test_wp05_rss_overlay_carries_mute_only(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("WP-05c")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchpointInvalid) as excinfo:
            await WatchpointRepository(session).create(
                family="rss",
                subject_key="rss:cluster:equities",
                display_name="Equities press cluster",
                effective_from=_T0,
                warn_threshold_pct=Decimal("85"),
            )
        assert excinfo.value.field == "warn_threshold_pct"


async def test_wp05_unknown_family_is_refused(app_engine: AsyncEngine, seed_tenant) -> None:
    """There is no ``pacing`` family (ADR-0116 Non-goals)."""
    tenant_id = await seed_tenant("WP-05d")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchpointInvalid) as excinfo:
            await WatchpointRepository(session).create(
                family="pacing",
                subject_key="pacing:whatever",
                display_name="Pacing",
                effective_from=_T0,
            )
        assert excinfo.value.field == "family"


# ---------------------------------------------------------------------------
# WP-06: the singleton rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "parameters"),
    [
        ("freshness", {"max_age_days": 120}),
        ("liquidity", {"horizon_months": 12, "min_coverage_ratio": Decimal("1.2")}),
    ],
)
async def test_wp06_a_second_identity_of_a_singleton_family_is_refused(
    app_engine: AsyncEngine, seed_tenant, family: str, parameters: dict
) -> None:
    tenant_id = await seed_tenant(f"WP-06-{family}")
    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)
        await repository.create(
            family=family,
            subject_key=f"{family}:first",
            display_name=f"{family} first",
            effective_from=_T0,
            **parameters,
        )
        with pytest.raises(WatchpointInvalid) as excinfo:
            await repository.create(
                family=family,
                subject_key=f"{family}:second",
                display_name=f"{family} second",
                effective_from=_T0,
                **parameters,
            )
        assert excinfo.value.field == "family"


async def test_wp06_versions_of_the_one_singleton_identity_stay_legal(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-06b")
    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)
        created = await _create_freshness(session)
        await repository.revise(
            created.watchpoint_id,
            effective_from=_T1,
            display_name="NAV freshness",
            max_age_days=90,
        )
        await repository.revise(
            created.watchpoint_id,
            effective_from=_T2,
            display_name="NAV freshness",
            max_age_days=60,
        )
        assert len(await repository.list_versions(created.watchpoint_id)) == 3
        assert len(await repository.effective_watchpoints(_T2)) == 1


async def test_wp06_a_future_dated_identity_still_counts_as_live(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``list_live_identities`` is unbounded in time, and must be.

    A singleton scheduled to start next week is still the tenant's one
    singleton. If the count question were asked as of "now", the seeder
    would see nothing, try to create a second, and the singleton rule
    would abort the whole seed step.
    """
    tenant_id = await seed_tenant("WP-06d")
    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)
        await _create_freshness(session, effective_from=_T2)

        assert await repository.effective_watchpoints(_T0) == []
        live = await repository.list_live_identities()
        assert [row.family for row in live] == ["freshness"]

        with pytest.raises(WatchpointInvalid):
            await _create_freshness(session, effective_from=_T0)


async def test_wp06_list_live_identities_drops_retired_ones(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-06e")
    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)
        created = await _create_freshness(session)
        assert len(await repository.list_live_identities()) == 1
        await repository.retire(created.watchpoint_id, effective_from=_T1)
        assert await repository.list_live_identities() == []


async def test_wp06_a_retired_singleton_frees_the_slot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WP-06c")
    async with tenant_context(app_engine, tenant_id) as session:
        repository = WatchpointRepository(session)
        first = await _create_freshness(session)
        await repository.retire(first.watchpoint_id, effective_from=_T1)

        replacement = await repository.create(
            family="freshness",
            subject_key="freshness:*",
            display_name="NAV freshness (new)",
            effective_from=_T2,
            max_age_days=45,
        )
        assert replacement.watchpoint_id != first.watchpoint_id
        assert [row.max_age_days for row in await repository.effective_watchpoints(_T2)] == [45]


# ---------------------------------------------------------------------------
# WP-07: RLS isolation
# ---------------------------------------------------------------------------


async def test_wp07_watchpoints_are_invisible_across_tenants(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant("WP-07-A")
    tenant_b = await seed_tenant("WP-07-B")

    async with tenant_context(app_engine, tenant_a) as session:
        created = await _create_freshness(session)

    async with tenant_context(app_engine, tenant_b) as session:
        repository = WatchpointRepository(session)
        assert await repository.effective_watchpoints(_T0) == []
        assert await repository.get_current(created.watchpoint_id) is None
        # Tenant B may create its own singleton: the rule is per tenant.
        await _create_freshness(session)
