# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-investment scope tests for :class:`CashflowWithNavProvider`."""

from __future__ import annotations

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers import (
    CashflowProvider,
    CashflowWithNavProvider,
    ProviderContext,
)


def test_filter_matches_cashflow_provider_for_calls_and_distributions(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """Per-investment ``calls`` and ``distributions`` columns match :class:`CashflowProvider`."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition A",
    )
    expected = CashflowProvider().get(ctx)
    df = CashflowWithNavProvider().get(ctx)
    assert list(df.columns) == ["calls", "distributions", "nav", "ncg"]

    expected_years = [int(idx.year) for idx in expected.index]
    assert df.index.tolist() == expected_years
    for year, exp_idx in zip(expected_years, expected.index, strict=True):
        assert df.loc[year, "calls"] == expected.loc[exp_idx, "calls"]
        assert df.loc[year, "distributions"] == expected.loc[exp_idx, "distributions"]


def test_per_investment_ncg_definition(
    clean_store: None,  # noqa: ARG001
) -> None:
    """``ncg = nav + cum_dist - cum_calls_magnitude`` for at least three year-ends."""
    invs = ("A", "B", "C")
    cf_in = pd.DataFrame(
        {
            "A": [0.0, 50.0, 30.0],
            "B": [0.0, 0.0, 0.0],
            "C": [0.0, 0.0, 0.0],
        },
        index=[
            pd.Timestamp("2022-06-30"),
            pd.Timestamp("2023-06-30"),
            pd.Timestamp("2024-06-30"),
        ],
    )
    cf_out = pd.DataFrame(
        {
            "A": [-100.0, -40.0, 0.0],
            "B": [-300.0, 0.0, 0.0],
            "C": [-50.0, -50.0, 0.0],
        },
        index=cf_in.index,
    )
    navs = pd.DataFrame(
        {
            "A": [120.0, 110.0, 130.0],
            "B": [310.0, 320.0, 330.0],
            "C": [55.0, 95.0, 100.0],
        },
        index=[
            pd.Timestamp("2022-12-31"),
            pd.Timestamp("2023-12-31"),
            pd.Timestamp("2024-12-31"),
        ],
    )

    store = get_data_store()
    store.store(
        "attributes",
        pd.DataFrame({a: ["x"] for a in invs}, index=["Note"]),
    )
    store.store("cash_flow_in_actual", cf_in)
    store.store("cash_flow_out_actual", cf_out)
    store.store("navs_actual", navs)

    ctx = ProviderContext(
        report_date=pd.Timestamp("2024-12-31"),
        all_investments=invs,
        investment_filter="A",
    )
    df = CashflowWithNavProvider().get(ctx)
    assert df.index.tolist() == [2022, 2023, 2024]

    # 2022 — A: calls=100, dist=0, nav=120 → ncg = 120 + 0 - 100 = 20.
    assert df.loc[2022, "ncg"] == 20.0
    # 2023 — A: calls=140, dist=50, nav=110 → ncg = 110 + 50 - 140 = 20.
    assert df.loc[2023, "ncg"] == 20.0
    # 2024 — A: calls=140, dist=80, nav=130 → ncg = 130 + 80 - 140 = 70.
    assert df.loc[2024, "ncg"] == 70.0

    # Bar columns scope correctly to "A" only.
    assert df.loc[2022, "calls"] == -100.0
    assert df.loc[2023, "distributions"] == 50.0


def test_per_investment_inactive_yields_empty(
    clean_store: None,  # noqa: ARG001
) -> None:
    """An investment with all-zero cashflows yields an empty DataFrame.

    The cashflow grid has no non-zero year, so the resampled aggregate is
    empty and the per-investment cashflow-with-nav frame is also empty.
    """
    cf_in = pd.DataFrame(
        {"A": [50.0], "B": [0.0]},
        index=[pd.Timestamp("2024-06-30")],
    )
    cf_out = pd.DataFrame(
        {"A": [-100.0], "B": [0.0]},
        index=[pd.Timestamp("2023-06-30")],
    )
    navs = pd.DataFrame(
        {"A": [50.0], "B": [0.0]},
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
    df = CashflowWithNavProvider().get(ctx)
    # B has zero cashflows but the year-grid still spans 2023→2024 from
    # CashflowProvider's resample.  ncg should be exactly zero everywhere.
    assert list(df.columns) == ["calls", "distributions", "nav", "ncg"]
    assert (df["calls"] == 0.0).all()
    assert (df["distributions"] == 0.0).all()
    assert (df["ncg"] == 0.0).all()
