# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the strategy, country, and sector breakdown providers."""

from __future__ import annotations

import pandas as pd
import pytest

from services.reporting.data_providers import (
    CountryProvider,
    ProviderContext,
    SectorProvider,
    StrategyProvider,
)


def test_strategy_portfolio_aggregates_by_subclass(
    populated_store_canonical: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """Each investment maps to a unique sub-class, so each share is the NAV share."""
    df = StrategyProvider().get(basic_ctx)
    # Total NAV = 130_000 + 230_000 + 60_000 = 420_000
    total = 420_000.0
    by_subclass = dict(zip(df["sub_class"], df["nav_share"], strict=True))
    assert by_subclass["Buyout"] == pytest.approx(130_000.0 / total, abs=1e-9)
    assert by_subclass["Energy"] == pytest.approx(230_000.0 / total, abs=1e-9)
    assert by_subclass["Direct Lending"] == pytest.approx(60_000.0 / total, abs=1e-9)
    assert df["nav_share"].sum() == pytest.approx(1.0, abs=1e-9)


def test_strategy_per_investment_yields_single_row(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """A single-investment scope returns one row with share=1.0."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition A",
    )
    df = StrategyProvider().get(ctx)
    assert df.shape[0] == 1
    assert df.iloc[0]["sub_class"] == "Buyout"
    assert df.iloc[0]["nav_share"] == 1.0


def test_country_portfolio_nav_weighted(
    populated_store_canonical: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """Country shares are NAV-weighted across all investments."""
    df = CountryProvider().get(basic_ctx)
    # Inputs: A: DE=1, US=0, NAV 130_000.  B: DE=0.5, US=0.5, NAV 230_000.
    #         C: DE=0, US=1, NAV 60_000.  Total NAV = 420_000.
    total = 420_000.0
    expected_de = (1.0 * 130_000 + 0.5 * 230_000 + 0.0 * 60_000) / total
    expected_us = (0.0 * 130_000 + 0.5 * 230_000 + 1.0 * 60_000) / total
    by_country = dict(zip(df["category"], df["share"], strict=True))
    assert by_country["DE"] == pytest.approx(expected_de, abs=1e-9)
    assert by_country["US"] == pytest.approx(expected_us, abs=1e-9)


def test_country_per_investment_passthrough(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """Per-investment country breakdown returns this investment's raw weights."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition B",
    )
    df = CountryProvider().get(ctx)
    by_country = dict(zip(df["category"], df["share"], strict=True))
    assert by_country == {"DE": 0.5, "US": 0.5}


def test_sector_portfolio_nav_weighted(
    populated_store_canonical: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """Sector shares are NAV-weighted and Healthcare reflects only A's NAV share."""
    df = SectorProvider().get(basic_ctx)
    total = 420_000.0
    expected_tech = 0.6 * 130_000 / total
    expected_healthcare = 0.4 * 130_000 / total
    expected_energy = 1.0 * 230_000 / total
    by_sector = dict(zip(df["category"], df["share"], strict=True))
    assert by_sector["Tech"] == pytest.approx(expected_tech, abs=1e-9)
    assert by_sector["Healthcare"] == pytest.approx(expected_healthcare, abs=1e-9)
    assert by_sector["Energy"] == pytest.approx(expected_energy, abs=1e-9)


def test_sector_per_investment_passthrough(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """Per-investment sector breakdown returns the investment's raw weights."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition A",
    )
    df = SectorProvider().get(ctx)
    by_sector = dict(zip(df["category"], df["share"], strict=True))
    assert by_sector == {"Tech": 0.6, "Healthcare": 0.4}
