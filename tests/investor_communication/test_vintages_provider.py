# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.data_providers.VintagesProvider`."""

from __future__ import annotations

import pandas as pd
import pytest

from core.data_store import get_data_store
from services.reporting.data_providers import ProviderContext, VintagesProvider


def test_groups_by_vintage_year_and_counts_investments(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Three investments with vintages 2018, 2018, 2020 group correctly."""
    invs = ("A", "B", "C")
    attributes = pd.DataFrame(
        {
            "A": [2018],
            "B": [2018],
            "C": [2020],
        },
        index=["Vintage Year"],
    )
    navs = pd.DataFrame(
        {"A": [100.0], "B": [100.0], "C": [200.0]},
        index=[pd.Timestamp("2024-12-31")],
    )

    store = get_data_store()
    store.store("attributes", attributes)
    store.store(
        "cash_flow_in_actual",
        pd.DataFrame({a: [0.0] for a in invs}, index=[pd.Timestamp("2024-01-01")]),
    )
    store.store(
        "cash_flow_out_actual",
        pd.DataFrame({a: [0.0] for a in invs}, index=[pd.Timestamp("2024-01-01")]),
    )
    store.store("navs_actual", navs)

    ctx = ProviderContext(
        report_date=pd.Timestamp("2024-12-31"),
        all_investments=invs,
        investment_filter=None,
    )
    df = VintagesProvider().get(ctx)
    assert df.index.tolist() == [2018, 2020]
    # Total NAV = 400. 2018 has A+B=200/400=0.5; 2020 has C=200/400=0.5.
    assert df.loc[2018, "nav_share"] == pytest.approx(0.5)
    assert df.loc[2020, "nav_share"] == pytest.approx(0.5)
    assert df.loc[2018, "investment_count"] == 2
    assert df.loc[2020, "investment_count"] == 1


def test_filter_returns_empty(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """Per-investment filter yields an empty DataFrame (portfolio-only)."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition A",
    )
    df = VintagesProvider().get(ctx)
    assert df.empty


def test_missing_vintage_row_returns_empty(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Without ``Vintage Year`` row the provider returns an empty DataFrame."""
    store = get_data_store()
    store.store(
        "attributes",
        pd.DataFrame({"A": ["Buyout"]}, index=["Investment Sub-Class"]),
    )
    store.store(
        "navs_actual",
        pd.DataFrame({"A": [100.0]}, index=[pd.Timestamp("2024-12-31")]),
    )
    ctx = ProviderContext(
        report_date=pd.Timestamp("2024-12-31"),
        all_investments=("A",),
        investment_filter=None,
    )
    df = VintagesProvider().get(ctx)
    assert df.empty
