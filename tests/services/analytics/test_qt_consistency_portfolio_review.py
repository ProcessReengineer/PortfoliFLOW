# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""QT-consistency regression for the sub-stream 5e portfolio aggregations.

The QT report-engine providers under
``services/reporting/data_providers/`` and the Phase-5e analytics
layer at ``services/analytics/portfolio_aggregation.py`` must yield
the same per-year multiples, cashflows, vintage shares, and header
KPIs to within ``1e-6``.

This test pins that contract by:

1. Building a deterministic two-investment fixture in the canonical
   QT shape: Excel-import-derived DataFrames seeded into the in-memory
   :class:`core.data_store.DataStore`. The same fixture is also
   converted to the per-investment dicts the new aggregations
   accept.
2. Calling the QT providers (``InvestedNavProvider``,
   ``CashflowWithNavProvider``, ``MultiplesTimeseriesProvider``,
   ``VintagesProvider``, ``KeyFiguresProvider``).
3. Calling the parallel
   :mod:`services.analytics.portfolio_aggregation` functions.
4. Asserting per-year and header-level numerical agreement.

The test does not exercise the region / sector breakdowns — those
have no QT counterpart with the new ORM-driven inputs (the QT
provider reads attributes from the import-format ``Attributes`` sheet, not from
the per-investment ``investment_region_weights`` rows). Region and
sector aggregations are covered by the unit tests under
``test_portfolio_aggregation.py``.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.data_store import get_data_store
from core.repositories.investment_repository import InvestmentDTO
from services.analytics.portfolio_aggregation import (
    aggregate_invested_capital_and_nav,
    aggregate_portfolio_cashflows,
    aggregate_portfolio_multiples,
    aggregate_vintage_distribution,
)
from services.reporting.data_providers import (
    CashflowWithNavProvider,
    InvestedNavProvider,
    KeyFiguresProvider,
    MultiplesTimeseriesProvider,
    ProviderContext,
    VintagesProvider,
)
from services.reporting.data_providers.cashflow_provider import CashflowProvider

_QT_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# Deterministic test universe
# ---------------------------------------------------------------------------


def _make_investment_dto(name: str, vintage_year: int) -> InvestmentDTO:
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return InvestmentDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        name=name,
        investment_type="private_equity",
        asset_class_id=uuid4(),
        manager_name=None,
        region=None,
        currency="EUR",
        vintage_year=vintage_year,
        commitment_amount=None,
        is_active=True,
        type_specific_data=None,
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


def _seed_universe() -> tuple[
    list[InvestmentDTO],
    dict[UUID, pd.Series],
    dict[UUID, pd.DataFrame],
    pd.Timestamp,
]:
    """Seed both the DataStore (QT path) and ORM-style dicts (web path).

    Returns:
        Investments, NAV history dict, cashflow dict, and the
        as-of date.
    """
    inv_a = _make_investment_dto("Alpha", vintage_year=2018)
    inv_b = _make_investment_dto("Beta", vintage_year=2020)

    # Date-indexed shapes consumed by the Excel-import providers.
    nav_dates = pd.to_datetime(
        [
            date(2023, 12, 31),
            date(2024, 6, 30),
            date(2024, 12, 31),
        ]
    )
    cf_dates = pd.to_datetime(
        [
            date(2023, 1, 15),
            date(2024, 3, 15),
            date(2024, 9, 15),
        ]
    )

    # ----- DataStore (QT) ---------------------------------------------------
    df_attr = pd.DataFrame(
        {
            "Alpha": ["2018"],
            "Beta": ["2020"],
        },
        index=["Vintage Year"],
    )
    df_nav = pd.DataFrame(
        {
            "Alpha": [100.0, 110.0, 130.0],
            "Beta": [float("nan"), 200.0, 220.0],
        },
        index=nav_dates,
    )
    df_cf_in = pd.DataFrame(
        {
            "Alpha": [0.0, 0.0, 20.0],
            "Beta": [0.0, 0.0, 0.0],
        },
        index=cf_dates,
    )
    df_cf_out = pd.DataFrame(
        {
            "Alpha": [-100.0, 0.0, 0.0],
            "Beta": [0.0, -200.0, 0.0],
        },
        index=cf_dates,
    )

    store = get_data_store()
    store.clear()
    store.store("attributes", df_attr)
    store.store("navs_actual", df_nav)
    store.store("cash_flow_in_actual", df_cf_in)
    store.store("cash_flow_out_actual", df_cf_out)

    # ----- ORM-style (web) -------------------------------------------------
    nav_history_by_inv: dict[UUID, pd.Series] = {
        inv_a.id: pd.Series(
            df_nav["Alpha"].dropna().values,
            index=df_nav["Alpha"].dropna().index,
        ),
        inv_b.id: pd.Series(
            df_nav["Beta"].dropna().values,
            index=df_nav["Beta"].dropna().index,
        ),
    }
    # Cashflows: emit one row per non-zero cell, signed.
    rows_a: list[dict] = []
    rows_b: list[dict] = []
    for ts in cf_dates:
        a_in = float(df_cf_in.loc[ts, "Alpha"])
        if a_in != 0.0:
            rows_a.append({"flow_timestamp": ts.tz_localize("UTC"), "amount": a_in})
        a_out = float(df_cf_out.loc[ts, "Alpha"])
        if a_out != 0.0:
            rows_a.append({"flow_timestamp": ts.tz_localize("UTC"), "amount": a_out})
        b_in = float(df_cf_in.loc[ts, "Beta"])
        if b_in != 0.0:
            rows_b.append({"flow_timestamp": ts.tz_localize("UTC"), "amount": b_in})
        b_out = float(df_cf_out.loc[ts, "Beta"])
        if b_out != 0.0:
            rows_b.append({"flow_timestamp": ts.tz_localize("UTC"), "amount": b_out})
    cashflows_by_inv: dict[UUID, pd.DataFrame] = {
        inv_a.id: pd.DataFrame(rows_a),
        inv_b.id: pd.DataFrame(rows_b),
    }

    report_date = pd.Timestamp("2024-12-31")
    return (
        [inv_a, inv_b],
        nav_history_by_inv,
        cashflows_by_inv,
        report_date,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_invested_capital_nav_matches_qt() -> None:
    investments, nav_dict, cf_dict, report_date = _seed_universe()
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=("Alpha", "Beta"),
        investment_filter=None,
    )
    qt_df = InvestedNavProvider().get(ctx)
    web = aggregate_invested_capital_and_nav(
        investments,
        nav_dict,
        cf_dict,
        report_date=report_date.date(),
    )
    qt_years = list(qt_df.index)
    assert web.years == qt_years
    for y, expected in zip(qt_years, web.invested_capital):
        assert qt_df.loc[y, "invested_capital"] == pytest.approx(expected, abs=_QT_TOLERANCE)
    for y, expected in zip(qt_years, web.nav):
        assert qt_df.loc[y, "nav"] == pytest.approx(expected, abs=_QT_TOLERANCE)


def test_yearly_cashflows_matches_qt() -> None:
    _investments, nav_dict, cf_dict, report_date = _seed_universe()
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=("Alpha", "Beta"),
        investment_filter=None,
    )
    qt_df = CashflowWithNavProvider(CashflowProvider()).get(ctx)
    web = aggregate_portfolio_cashflows(cf_dict, nav_dict, report_date=report_date.date())
    qt_years = list(qt_df.index)
    assert web.years == qt_years
    for y, c, d, n, ncg in zip(qt_years, web.calls, web.distributions, web.nav, web.ncg):
        assert qt_df.loc[y, "calls"] == pytest.approx(c, abs=_QT_TOLERANCE)
        assert qt_df.loc[y, "distributions"] == pytest.approx(d, abs=_QT_TOLERANCE)
        assert qt_df.loc[y, "nav"] == pytest.approx(n, abs=_QT_TOLERANCE)
        assert qt_df.loc[y, "ncg"] == pytest.approx(ncg, abs=_QT_TOLERANCE)


def test_multiples_matches_qt() -> None:
    _investments, nav_dict, cf_dict, report_date = _seed_universe()
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=("Alpha", "Beta"),
        investment_filter=None,
    )
    qt_df = MultiplesTimeseriesProvider().get(ctx)
    web = aggregate_portfolio_multiples(cf_dict, nav_dict, report_date=report_date.date())
    qt_years = list(qt_df.index)
    assert web.years == qt_years
    for y, dpi, rvpi, tvpi, irr in zip(qt_years, web.dpi, web.rvpi, web.tvpi, web.irr):
        _approx_equal(qt_df.loc[y, "dpi"], dpi)
        _approx_equal(qt_df.loc[y, "rvpi"], rvpi)
        _approx_equal(qt_df.loc[y, "tvpi"], tvpi)
        _approx_equal(qt_df.loc[y, "irr"], irr)


def test_vintage_distribution_matches_qt() -> None:
    investments, nav_dict, _cf_dict, report_date = _seed_universe()
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=("Alpha", "Beta"),
        investment_filter=None,
    )
    qt_df = VintagesProvider().get(ctx)

    nav_by_inv = {
        inv.id: float(nav_dict[inv.id].iloc[-1])
        for inv in investments
        if not nav_dict[inv.id].empty
    }
    web = aggregate_vintage_distribution(investments, nav_by_inv)

    # Both must list the same years.
    assert web.vintages == [int(y) for y in qt_df.index]
    # nav_share is fraction; web returns percent.
    for y, weight in zip(web.vintages, web.weight_pct):
        qt_share = float(qt_df.loc[y, "nav_share"]) * 100.0
        assert weight == pytest.approx(qt_share, abs=_QT_TOLERANCE)
    for y, count in zip(web.vintages, web.count):
        assert count == int(qt_df.loc[y, "investment_count"])


def test_header_kpis_match_qt() -> None:
    """The four header scalars (NAV, IRR, TVPI, DPI) must match the
    QT KeyFiguresProvider output."""
    from services.portfolio_review import PortfolioReviewService

    investments, nav_dict, cf_dict, report_date = _seed_universe()
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=("Alpha", "Beta"),
        investment_filter=None,
    )
    qt_kf = KeyFiguresProvider().get(ctx)

    # Reproduce the service's header logic without DB plumbing.
    nav_by_inv = {
        inv.id: float(nav_dict[inv.id].iloc[-1])
        for inv in investments
        if not nav_dict[inv.id].empty
    }
    cf_in_by_inv: dict[UUID, pd.Series] = {}
    cf_out_by_inv: dict[UUID, pd.Series] = {}
    for inv in investments:
        df = cf_dict[inv.id]
        if df.empty:
            empty_idx = pd.DatetimeIndex([], tz="UTC")
            cf_in_by_inv[inv.id] = pd.Series(dtype="float64", index=empty_idx)
            cf_out_by_inv[inv.id] = pd.Series(dtype="float64", index=empty_idx)
            continue
        cf_in_by_inv[inv.id] = (
            df.loc[df["amount"] > 0.0].groupby("flow_timestamp")["amount"].sum().sort_index()
        )
        cf_out_by_inv[inv.id] = (
            df.loc[df["amount"] < 0.0].groupby("flow_timestamp")["amount"].sum().sort_index()
        )

    header = PortfolioReviewService._build_portfolio_header(
        cf_in_by_inv, cf_out_by_inv, nav_by_inv, report_date.date()
    )

    _approx_equal(qt_kf.nav_eur, header.nav_eur)
    _approx_equal(qt_kf.tvpi, header.tvpi)
    _approx_equal(qt_kf.dpi, header.dpi)
    _approx_equal(qt_kf.irr, header.irr)


def _approx_equal(qt_value: float | None, web_value: float | None) -> None:
    """NaN-and-None aware approximate equality at the 1e-6 tolerance."""
    if qt_value is None and web_value is None:
        return
    if qt_value is None or web_value is None:
        # The QT layer uses 0.0-as-None for NAV; treat 0/None pair as equal.
        if (qt_value or 0.0) == 0.0 and (web_value or 0.0) == 0.0:
            return
        raise AssertionError(f"None / value mismatch: qt={qt_value!r}, web={web_value!r}")
    if math.isnan(qt_value) and math.isnan(web_value):
        return
    if math.isnan(qt_value) or math.isnan(web_value):
        raise AssertionError(f"NaN mismatch: qt={qt_value!r}, web={web_value!r}")
    assert qt_value == pytest.approx(web_value, abs=_QT_TOLERANCE)
