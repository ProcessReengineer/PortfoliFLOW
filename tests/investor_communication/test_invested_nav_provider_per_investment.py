# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-investment scope tests for :class:`InvestedNavProvider`.

These tests cover the new behaviour added when the provider was extended
to support ``ctx.investment_filter != None``.  Portfolio-mode tests stay
in :mod:`test_invested_nav_provider`.
"""

from __future__ import annotations

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers import (
    InvestedNavProvider,
    ProviderContext,
)


def test_per_investment_yearly_invested_and_nav(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Single-investment fixture yields the correct year range and values."""
    invs = ("A", "B")
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
            "B": [-200.0, 0.0, -10.0],
        },
        index=cf_in.index,
    )
    navs = pd.DataFrame(
        {
            "A": [80.0, 120.0, 130.0],
            "B": [None, 25.0, 60.0],
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
        investment_filter="A",
    )
    df = InvestedNavProvider().get(ctx)
    assert list(df.columns) == ["invested_capital", "nav"]
    assert df.index.tolist() == [2022, 2023, 2024]
    # A's calls: 100 in 2022, 50 in 2023, 0 in 2024.
    assert df.loc[2022, "invested_capital"] == 100.0
    assert df.loc[2023, "invested_capital"] == 150.0
    assert df.loc[2024, "invested_capital"] == 150.0
    # A's NAVs.
    assert df.loc[2022, "nav"] == 80.0
    assert df.loc[2023, "nav"] == 120.0
    assert df.loc[2024, "nav"] == 130.0


def test_per_investment_no_calls_with_nav_only(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Investment with no calls but with NAV — year range starts at first NAV year."""
    invs = ("X",)
    cf_in = pd.DataFrame(
        {"X": [0.0, 0.0]},
        index=[pd.Timestamp("2023-06-30"), pd.Timestamp("2024-06-30")],
    )
    cf_out = pd.DataFrame(
        {"X": [0.0, 0.0]},
        index=cf_in.index,
    )
    navs = pd.DataFrame(
        {"X": [40.0, 50.0]},
        index=[pd.Timestamp("2023-12-31"), pd.Timestamp("2024-12-31")],
    )

    store = get_data_store()
    store.store("attributes", pd.DataFrame({"X": ["x"]}, index=["Note"]))
    store.store("cash_flow_in_actual", cf_in)
    store.store("cash_flow_out_actual", cf_out)
    store.store("navs_actual", navs)

    ctx = ProviderContext(
        report_date=pd.Timestamp("2024-12-31"),
        all_investments=invs,
        investment_filter="X",
    )
    df = InvestedNavProvider().get(ctx)
    assert df.index.tolist() == [2023, 2024]
    assert df.loc[2023, "invested_capital"] == 0.0
    assert df.loc[2024, "invested_capital"] == 0.0
    assert df.loc[2023, "nav"] == 40.0
    assert df.loc[2024, "nav"] == 50.0


def test_per_investment_missing_from_both_sheets(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Investment column missing from both source DataFrames yields empty."""
    cf_in = pd.DataFrame(
        {"A": [0.0]},
        index=[pd.Timestamp("2024-01-01")],
    )
    cf_out = pd.DataFrame(
        {"A": [-100.0]},
        index=[pd.Timestamp("2023-01-01")],
    )
    navs = pd.DataFrame(
        {"A": [50.0]},
        index=[pd.Timestamp("2024-12-31")],
    )

    store = get_data_store()
    store.store(
        "attributes",
        pd.DataFrame({"A": ["x"], "B": ["y"]}, index=["Note"]),
    )
    store.store("cash_flow_in_actual", cf_in)
    store.store("cash_flow_out_actual", cf_out)
    store.store("navs_actual", navs)

    ctx = ProviderContext(
        report_date=pd.Timestamp("2024-12-31"),
        all_investments=("A", "B"),
        investment_filter="B",
    )
    df = InvestedNavProvider().get(ctx)
    assert df.empty
    assert list(df.columns) == ["invested_capital", "nav"]
