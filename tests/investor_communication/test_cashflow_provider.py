# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.data_providers.CashflowProvider`."""

from __future__ import annotations

import pandas as pd

from services.reporting.data_providers import CashflowProvider, ProviderContext


def test_portfolio_aggregate_yearly(
    populated_store_canonical: None,  # noqa: ARG001 — fixture seeds the DataStore
    basic_ctx: ProviderContext,
) -> None:
    """Portfolio-level aggregation sums across all investments per year."""
    provider = CashflowProvider()
    df = provider.get(basic_ctx)
    assert list(df.columns) == ["calls", "distributions"]
    assert len(df) == 1
    row = df.iloc[0]
    # Calls: A: -150_000, B: -200_000, C: -80_000 → -430_000
    assert row["calls"] == -430_000.0
    # Distributions: A: 80_000, C: 30_000 → 110_000
    assert row["distributions"] == 110_000.0


def test_per_investment(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """Per-investment scoping returns only that investment's cashflows."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition A",
    )
    df = CashflowProvider().get(ctx)
    assert df.iloc[0]["calls"] == -150_000.0
    assert df.iloc[0]["distributions"] == 80_000.0


def test_sign_normalisation_positive_out(
    populated_store_positive_out: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """When CF Out is stored with positive values, calls still come out negative."""
    df = CashflowProvider().get(basic_ctx)
    assert df.iloc[0]["calls"] == -430_000.0


def test_empty_store_returns_empty_dataframe(
    clean_store: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """No data in the store yields an empty DataFrame with expected columns."""
    df = CashflowProvider().get(basic_ctx)
    assert df.empty
    assert list(df.columns) == ["calls", "distributions"]
