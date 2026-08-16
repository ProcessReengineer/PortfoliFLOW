# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.data_providers.InvestedNavProvider`."""

from __future__ import annotations

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers import (
    InvestedNavProvider,
    ProviderContext,
)


def test_portfolio_aggregate_invested_and_nav(
    populated_store_canonical: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """Year-end invested capital is cumulative call magnitude across all investments."""
    df = InvestedNavProvider().get(basic_ctx)
    assert list(df.columns) == ["invested_capital", "nav"]
    # All calls happen in 2024; report_date is 2024-12-31. Single year row.
    assert df.index.tolist() == [2024]
    # Calls magnitude: A (100k+50k) + B (200k) + C (80k) = 430_000.
    assert df.iloc[0]["invested_capital"] == 430_000.0
    # NAV at 2024-12-31: 130_000 + 230_000 + 60_000 = 420_000.
    assert df.iloc[0]["nav"] == 420_000.0


def test_filter_scopes_to_single_investment(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """Per-investment filter scopes the result to one investment."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition A",
    )
    df = InvestedNavProvider().get(ctx)
    # Investition A: calls 100k + 50k in 2024; NAV at 2024-12-31 is 130k.
    assert list(df.columns) == ["invested_capital", "nav"]
    assert df.index.tolist() == [2024]
    assert df.loc[2024, "invested_capital"] == 150_000.0
    assert df.loc[2024, "nav"] == 130_000.0


def test_multi_year_known_fixture(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Synthetic 3-investment fixture with multiple years yields known totals."""
    invs = ("A", "B", "C")
    cf_in = pd.DataFrame(
        {a: [0.0, 0.0, 0.0] for a in invs},
        index=[
            pd.Timestamp("2022-06-30"),
            pd.Timestamp("2023-06-30"),
            pd.Timestamp("2024-06-30"),
        ],
    )
    cf_out = pd.DataFrame(
        {
            "A": [-100.0, -50.0, 0.0],
            "B": [0.0, -30.0, -20.0],
            "C": [-40.0, 0.0, 0.0],
        },
        index=cf_in.index,
    )
    navs = pd.DataFrame(
        {
            "A": [80.0, 120.0, 130.0],
            "B": [None, 25.0, 60.0],
            "C": [35.0, 40.0, 45.0],
        },
        index=[
            pd.Timestamp("2022-12-31"),
            pd.Timestamp("2023-12-31"),
            pd.Timestamp("2024-12-31"),
        ],
    )

    store = get_data_store()
    store.store("attributes", pd.DataFrame({a: ["x"] for a in invs}, index=["Note"]))
    store.store("cash_flow_in_actual", cf_in)
    store.store("cash_flow_out_actual", cf_out)
    store.store("navs_actual", navs)

    ctx = ProviderContext(
        report_date=pd.Timestamp("2024-12-31"),
        all_investments=invs,
        investment_filter=None,
    )
    df = InvestedNavProvider().get(ctx)

    assert df.index.tolist() == [2022, 2023, 2024]
    assert df.loc[2022, "invested_capital"] == 140.0  # 100 + 40
    assert df.loc[2023, "invested_capital"] == 220.0  # 140 + 50 + 30
    assert df.loc[2024, "invested_capital"] == 240.0  # 220 + 20

    # NAVs end-of-year totals.
    assert df.loc[2022, "nav"] == 115.0  # 80 + 35 (B is NaN)
    assert df.loc[2023, "nav"] == 185.0  # 120 + 25 + 40
    assert df.loc[2024, "nav"] == 235.0  # 130 + 60 + 45
