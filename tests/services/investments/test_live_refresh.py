# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Refresh-core tests against the synthetic adapter (ADR-0093, #036 slice 5).

Exercises ``services.investments.live_refresh.refresh_tenant_live_data`` end
to end against the live compose Postgres, using the fixture-driven synthetic
provider (forced via ``forced_provider="synthetic"``) so no live network is
touched — mirroring the network-free posture of ``tests/cli/test_irene_tick.py``.

Strand S3 (ADR-0097 §9 + ADR-0098 §4) retired the interim S0 per-share
suppression. Live-series eligibility now additionally requires
``valuation_mode='unitised'``, so a ``'reported'`` listed instrument is skipped
before any fetch (the market-linked gate), and for the ``'unitised'`` ones the
write path re-routes per-share series correctly: ``nav_price`` → instrument
prices (materialised into ``'system'`` NAVs in the same transaction) and
per-share ``dividend`` → scaled by holdings into a position-level cashflow.

Coverage:

* an eligible **unitised** investment (``listed_equity`` + primary ticker +
  an opening position) has its ``nav_price`` written to ``instrument_prices``
  and materialised into a ``'system'`` NAV, its per-share ``dividend`` scaled
  by holdings, and its position-level ``distribution`` ingested — all
  attributed to the tenant system actor;
* an ineligible investment (``private_equity``) is left untouched;
* one unitised investment's provider failure (unresolvable ticker) is
  contained — counted, and the tenant refresh completes for the others;
* the fetch window's lower bound derives from ``last_run_at`` (a point before
  the last run is filtered out), leaving nothing to ingest or materialise.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InstrumentPriceRepository,
    InvestmentCashflowRepository,
    InvestmentIdentifierRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentService
from services.investments import live_refresh as live_refresh_mod
from services.investments.live_refresh import (
    MARKET_DATA_SYSTEM_ACTOR_EMAIL,
    refresh_tenant_live_data,
)
from services.market_data.dto import (
    NormalizedSeries,
    SeriesKind,
    SeriesPoint,
)
from services.market_data.factory import build_adapter as _real_build_adapter

# A fixed "now" so the fetch windows are deterministic. The fixture dates
# below sit a few days before it, comfortably inside the 30-day fallback.
_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
_NAV_DATE = "2026-07-01"
_DIV_DATE = "2026-07-02"
_DIST_DATE = "2026-07-03"
# The position opens before every fixture date, so holdings are 100 throughout.
_OPEN_DATE = date(2026, 6, 1)


async def _seed_system_actor(app_engine: AsyncEngine, tenant_id: UUID) -> UUID:
    """Create the market-data system actor (inactive) and return its id."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email=MARKET_DATA_SYSTEM_ACTOR_EMAIL,
            password_hash="x" * 16,
            roles=("auditor",),
            is_active=False,
        )
    return actor.id


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    actor_id: UUID,
    *,
    name: str,
    investment_type: str,
    ticker: str | None,
    unitised: bool = False,
    opening_units: str | None = None,
) -> UUID:
    """Create an investment; optionally flip it unitised and open a position.

    Attaches a primary ticker identifier if given. When ``unitised`` is set,
    flips ``valuation_mode`` (the operator act of ADR-0097 §6, done here
    directly) and, if ``opening_units`` is given, seeds one ``opening``
    transaction so holdings exist for materialisation / scaling.
    """
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        ac = await AssetClassRepository(session).get_by_code("ac_" + name)
        if ac is None:
            ac = await AssetClassRepository(session).create(code="ac_" + name, display_name=name)
        inv = await InvestmentRepository(session).create(
            name=name,
            investment_type=investment_type,
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor_id,
        )
        if ticker is not None:
            await InvestmentIdentifierRepository(session).add(
                investment_id=inv.id,
                scheme="ticker",
                value=ticker,
                created_by=actor_id,
                is_primary=True,
            )
        if unitised:
            await session.execute(
                text("UPDATE investments SET valuation_mode = 'unitised' WHERE id = :id"),
                {"id": inv.id},
            )

    if opening_units is not None:
        async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
            service = InvestmentService(
                investments=InvestmentRepository(session),
                navs=InvestmentNavRepository(session),
                cashflows=InvestmentCashflowRepository(session),
                position_transactions=PositionTransactionRepository(session),
                instrument_prices=InstrumentPriceRepository(session),
            )
            await service.add_position_transaction(
                investment_id=inv.id,
                txn_type="opening",
                trade_date=_OPEN_DATE,
                units=Decimal(opening_units),
                currency="EUR",
                ingest_origin="manual",
                created_by=actor_id,
            )
    return inv.id


def _write_fixture(tmp_path: Path) -> Path:
    # nav_price + dividend are per-share (routed for the unitised investment);
    # distribution is a position-level kind.
    fixture = {
        "ACME": {
            "nav_price": [[_NAV_DATE, "101.50"]],
            "dividend": [[_DIV_DATE, "1.25"]],
            "distribution": [[_DIST_DATE, "5000.0000"]],
        }
    }
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_refresh_routes_unitised_skips_ineligible_and_contains_error(
    app_engine: AsyncEngine,
    seed_tenant,
    tmp_path: Path,
    monkeypatch,
) -> None:
    tenant_id = await seed_tenant()
    actor_id = await _seed_system_actor(app_engine, tenant_id)

    eligible = await _seed_investment(
        app_engine,
        tenant_id,
        actor_id,
        name="Eligible",
        investment_type="listed_equity",
        ticker="ACME",
        unitised=True,
        opening_units="100",
    )
    # A reported listed instrument: market-linked type + primary ticker, but
    # NOT unitised — the ADR-0097 §9 gate skips it before any fetch.
    reported = await _seed_investment(
        app_engine,
        tenant_id,
        actor_id,
        name="Reported",
        investment_type="listed_equity",
        ticker="ACME3",
    )
    ineligible = await _seed_investment(
        app_engine,
        tenant_id,
        actor_id,
        name="Private",
        investment_type="private_equity",
        ticker="ACME2",
    )
    # A unitised eligible investment whose ticker is absent from the fixture —
    # its fetch raises IdentifierNotResolvableError, which must be contained.
    failing = await _seed_investment(
        app_engine,
        tenant_id,
        actor_id,
        name="Missing",
        investment_type="listed_equity",
        ticker="GHOST",
        unitised=True,
    )

    fixture_path = _write_fixture(tmp_path)
    monkeypatch.setenv("MARKET_DATA_SYNTHETIC_FIXTURE", str(fixture_path))

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        report = await refresh_tenant_live_data(
            session, now=_NOW, last_run_at=None, forced_provider="synthetic"
        )

    # Two unitised eligible investments considered (ACME + GHOST); the reported
    # and private ones are gated out before any fetch.
    assert report.considered == 2
    assert report.refreshed == 1  # only ACME ingested data
    assert report.errors == 1  # GHOST's unresolvable-identifier fetch
    # ACME: 1 price + 1 scaled dividend cashflow + 1 distribution cashflow.
    assert report.inserted == 3
    assert report.skipped_unit_mismatch == 0  # the gate keeps reported unfetched

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        prices = await InstrumentPriceRepository(session).list_by_investment(eligible)
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(
            eligible, "actual"
        )
        cashflows = await InvestmentCashflowRepository(session).list_by_investment(eligible)
        reported_navs = await InvestmentNavRepository(session).list_by_investment(reported)
        ineligible_navs = await InvestmentNavRepository(session).list_by_investment(ineligible)
        failing_navs = await InvestmentNavRepository(session).list_by_investment(failing)

    # nav_price landed in instrument_prices (never a live NAV row).
    assert len(prices) == 1
    assert prices[0].as_of_date == date(2026, 7, 1)
    assert prices[0].ingest_origin == "live"
    assert prices[0].source == "synthetic"

    # The NAV is materialised: holdings(100) × price(101.50) = 10150, 'system'.
    assert len(navs) == 1
    assert navs[0].as_of_date == date(2026, 7, 1)
    assert navs[0].ingest_origin == "system"
    assert navs[0].nav_value == Decimal("10150.0000")

    # Two live cashflows: the scaled dividend (1.25 × 100 = 125) and the
    # position-level distribution (5000).
    by_type = {cf.flow_type: cf for cf in cashflows}
    assert set(by_type) == {"dividend", "distribution"}
    assert by_type["dividend"].amount == Decimal("125.0000")
    assert by_type["dividend"].ingest_origin == "live"
    assert by_type["distribution"].amount == Decimal("5000.0000")
    assert all(cf.created_by == actor_id for cf in cashflows)

    # The reported, private, and unresolvable investments are untouched.
    assert reported_navs == []
    assert ineligible_navs == []
    assert failing_navs == []


async def test_window_lower_bound_derives_from_last_run_at(
    app_engine: AsyncEngine,
    seed_tenant,
    tmp_path: Path,
    monkeypatch,
) -> None:
    tenant_id = await seed_tenant()
    actor_id = await _seed_system_actor(app_engine, tenant_id)
    eligible = await _seed_investment(
        app_engine,
        tenant_id,
        actor_id,
        name="Eligible",
        investment_type="listed_equity",
        ticker="ACME",
        unitised=True,
        opening_units="100",
    )

    fixture_path = _write_fixture(tmp_path)
    monkeypatch.setenv("MARKET_DATA_SYNTHETIC_FIXTURE", str(fixture_path))

    # last_run_at is AFTER the fixture's points, so the window
    # [last_run_at.date(), today] excludes them — nothing is fetched, so
    # nothing is ingested or materialised.
    last_run = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        report = await refresh_tenant_live_data(
            session, now=_NOW, last_run_at=last_run, forced_provider="synthetic"
        )

    assert report.considered == 1
    assert report.inserted == 0  # all fixture points fall before the window
    assert report.refreshed == 0

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        prices = await InstrumentPriceRepository(session).list_by_investment(eligible)
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(
            eligible, "actual"
        )
    assert prices == []
    assert navs == []


class _FakeYahooAdapter:
    """A network-free stand-in for the yahoo adapter, for the unforced path.

    Returns a one-point ``nav_price`` series (so the ticker-primary unitised
    investment ingests real data on the unforced route) and an empty series for
    every other kind — the kind loop runs to completion without a live fetch.
    """

    async def fetch_series(self, ident, kind, window) -> NormalizedSeries:
        if kind is SeriesKind.NAV_PRICE:
            return NormalizedSeries(
                ident=ident,
                provider="yahoo",
                kind=SeriesKind.NAV_PRICE,
                currency="EUR",
                points=(SeriesPoint(as_of_date=date(2026, 7, 1), value=Decimal("101.50")),),
            )
        return NormalizedSeries(ident=ident, provider="yahoo", kind=kind, currency="EUR", points=())


async def test_unforced_refresh_skips_forced_only_synthetic_no_fixture(
    app_engine: AsyncEngine,
    seed_tenant,
    monkeypatch,
) -> None:
    """Regression for the demo defect (synthetic `routing: forced_only`).

    An eligible ticker-primary unitised investment is refreshed on the
    **unforced** path (``forced_provider=None``) with
    ``MARKET_DATA_SYNTHETIC_FIXTURE`` **unset**. Because synthetic is now
    ``forced_only``, the kinds no real provider serves (``coupon`` and the
    position-level flows) yield no route and the kind loop `continue`s past
    them — synthetic's adapter is never built, so the unset-fixture
    ``MarketDataConfigurationError`` never fires. The investment therefore
    completes with ``errors=0`` and counts as refreshed once its ``nav_price``
    (routed to yahoo, here a network-free fake) has ingested.

    Before the fix, ``coupon`` routed to synthetic (priority 0, full coverage);
    building it raised ``MarketDataConfigurationError`` inside the whole-loop
    ``try``, so every eligible investment ended ``errors=1, refreshed=0`` even
    after its ``nav_price`` had ingested.
    """
    tenant_id = await seed_tenant()
    actor_id = await _seed_system_actor(app_engine, tenant_id)
    eligible = await _seed_investment(
        app_engine,
        tenant_id,
        actor_id,
        name="Eligible",
        investment_type="listed_equity",
        ticker="ACME",
        unitised=True,
        opening_units="100",
    )

    # The fixture is unset: building the synthetic adapter WOULD raise. Only a
    # non-forced_only route must be built (yahoo, here faked network-free); a
    # synthetic build delegates to the real builder, so if a regression let
    # synthetic be routed again the test fails loudly with errors=1.
    monkeypatch.delenv("MARKET_DATA_SYNTHETIC_FIXTURE", raising=False)

    def _fake_build(name: str):
        if name == "yahoo":
            return _FakeYahooAdapter()
        return _real_build_adapter(name)

    monkeypatch.setattr(live_refresh_mod, "build_adapter", _fake_build)

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        report = await refresh_tenant_live_data(
            session, now=_NOW, last_run_at=None, forced_provider=None
        )

    assert report.considered == 1
    assert report.errors == 0  # no synthetic build → no contained error
    assert report.refreshed == 1  # nav_price ingested
    assert report.inserted >= 1

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        prices = await InstrumentPriceRepository(session).list_by_investment(eligible)
    assert len(prices) == 1
    assert prices[0].source == "yahoo"
    assert prices[0].as_of_date == date(2026, 7, 1)
