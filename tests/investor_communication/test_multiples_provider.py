# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.data_providers.MultiplesProvider`."""

from __future__ import annotations

import math

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers import MultiplesProvider, ProviderContext


def test_portfolio_multiples_match_canonical_inputs(
    populated_store_canonical: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """Each investment's TVPI and DPI agree with the analytic value."""
    df = MultiplesProvider().get(basic_ctx)
    assert list(df.index) == list(basic_ctx.all_investments)

    # A: calls 150_000, distributions 80_000, NAV 130_000 → TVPI 1.4, DPI 0.5333..
    assert df.loc["Investition A", "TVPI"] == (130_000 + 80_000) / 150_000
    assert df.loc["Investition A", "DPI"] == 80_000 / 150_000
    # B: calls 200_000, no distributions, NAV 230_000 → TVPI 1.15, DPI 0.0
    assert df.loc["Investition B", "TVPI"] == 230_000 / 200_000
    assert df.loc["Investition B", "DPI"] == 0.0


def test_per_investment_returns_single_row(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """Filtering by a single investment yields a one-row DataFrame."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition C",
    )
    df = MultiplesProvider().get(ctx)
    assert list(df.index) == ["Investition C"]


def test_no_calls_yields_nan(clean_store: None, basic_ctx: ProviderContext) -> None:  # noqa: ARG001
    """Investments with no calls are NaN, not divide-by-zero errors."""
    store = get_data_store()
    inv = list(basic_ctx.all_investments)
    store.store(
        "attributes",
        pd.DataFrame({c: ["X"] for c in inv}, index=["Investment Sub-Class"]),
    )
    store.store(
        "cash_flow_in_actual",
        pd.DataFrame({c: [0.0] for c in inv}, index=[pd.Timestamp("2024-06-30")]),
    )
    store.store(
        "cash_flow_out_actual",
        pd.DataFrame({c: [0.0] for c in inv}, index=[pd.Timestamp("2024-06-30")]),
    )
    store.store(
        "navs_actual",
        pd.DataFrame({c: [100.0] for c in inv}, index=[pd.Timestamp("2024-12-31")]),
    )
    df = MultiplesProvider().get(basic_ctx)
    for inv_name in inv:
        assert math.isnan(df.loc[inv_name, "TVPI"])
        assert math.isnan(df.loc[inv_name, "DPI"])


def test_index_order_preserved(
    populated_store_canonical: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """Result index equals ``ctx.all_investments`` exactly."""
    df = MultiplesProvider().get(basic_ctx)
    assert tuple(df.index) == basic_ctx.all_investments
