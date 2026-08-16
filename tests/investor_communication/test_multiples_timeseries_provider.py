# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.data_providers.MultiplesTimeseriesProvider`."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from core.data_store import get_data_store
from services.reporting.data_providers import (
    MultiplesTimeseriesProvider,
    ProviderContext,
)


def test_per_investment_filter_returns_data(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """Per-investment filter now scopes to a single investment."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition A",
    )
    df = MultiplesTimeseriesProvider().get(ctx)
    assert list(df.columns) == ["dpi", "rvpi", "tvpi", "irr"]
    assert not df.empty
    assert df.index.tolist() == [2024]
    # Investition A: calls 150k, dist 80k (30k+50k), NAV 130k.
    assert df.loc[2024, "dpi"] == pytest.approx(80_000.0 / 150_000.0)
    assert df.loc[2024, "rvpi"] == pytest.approx(130_000.0 / 150_000.0)
    assert df.loc[2024, "tvpi"] == pytest.approx((80_000.0 + 130_000.0) / 150_000.0)


def test_known_single_investment_yearly_multiples(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Single-investment example with analytically known DPI / RVPI / TVPI per year."""
    inv = ("A",)
    # Capital call of 100 at year 0 (2022), distribution of 10 at year 2 (2024).
    cf_in = pd.DataFrame(
        {"A": [0.0, 10.0]},
        index=[pd.Timestamp("2022-06-30"), pd.Timestamp("2024-06-30")],
    )
    cf_out = pd.DataFrame(
        {"A": [-100.0]},
        index=[pd.Timestamp("2022-06-30")],
    )
    navs = pd.DataFrame(
        {"A": [120.0, 130.0, 140.0]},
        index=[
            pd.Timestamp("2022-12-31"),
            pd.Timestamp("2023-12-31"),
            pd.Timestamp("2024-12-31"),
        ],
    )

    store = get_data_store()
    store.store("attributes", pd.DataFrame({"A": ["x"]}, index=["Note"]))
    store.store("cash_flow_in_actual", cf_in)
    store.store("cash_flow_out_actual", cf_out)
    store.store("navs_actual", navs)

    ctx = ProviderContext(
        report_date=pd.Timestamp("2024-12-31"),
        all_investments=inv,
        investment_filter=None,
    )
    df = MultiplesTimeseriesProvider().get(ctx)
    assert df.index.tolist() == [2022, 2023, 2024]

    # 2022: NAV=120, dist=0, calls=100 → DPI=0.0, RVPI=1.20, TVPI=1.20.
    assert df.loc[2022, "dpi"] == pytest.approx(0.0)
    assert df.loc[2022, "rvpi"] == pytest.approx(1.20)
    assert df.loc[2022, "tvpi"] == pytest.approx(1.20)

    # 2023: NAV=130, dist=0, calls=100 → DPI=0.0, RVPI=1.30, TVPI=1.30.
    assert df.loc[2023, "dpi"] == pytest.approx(0.0)
    assert df.loc[2023, "rvpi"] == pytest.approx(1.30)
    assert df.loc[2023, "tvpi"] == pytest.approx(1.30)

    # 2024: NAV=140, dist=10, calls=100 → DPI=0.10, RVPI=1.40, TVPI=1.50.
    assert df.loc[2024, "dpi"] == pytest.approx(0.10)
    assert df.loc[2024, "rvpi"] == pytest.approx(1.40)
    assert df.loc[2024, "tvpi"] == pytest.approx(1.50)

    # IRR converges and is finite for at least 2024.
    assert math.isfinite(df.loc[2024, "irr"])


def test_per_investment_isolated_scenario(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Per-investment filter on a multi-investment store yields the same multiples
    as the same investment in isolation (the noise from other investments is gone).
    """
    invs = ("A", "B")
    cf_in = pd.DataFrame(
        {
            "A": [0.0, 10.0],
            "B": [50.0, 0.0],
        },
        index=[pd.Timestamp("2022-06-30"), pd.Timestamp("2024-06-30")],
    )
    cf_out = pd.DataFrame(
        {
            "A": [-100.0, 0.0],
            "B": [-200.0, 0.0],
        },
        index=cf_in.index,
    )
    navs = pd.DataFrame(
        {
            "A": [120.0, 130.0, 140.0],
            "B": [220.0, 230.0, 240.0],
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
    df = MultiplesTimeseriesProvider().get(ctx)

    # A in isolation: calls=100, dist=10@2024, NAV trajectory 120/130/140.
    assert df.index.tolist() == [2022, 2023, 2024]
    assert df.loc[2022, "rvpi"] == pytest.approx(1.20)
    assert df.loc[2024, "dpi"] == pytest.approx(0.10)
    assert df.loc[2024, "rvpi"] == pytest.approx(1.40)
    assert df.loc[2024, "tvpi"] == pytest.approx(1.50)
    assert math.isfinite(df.loc[2024, "irr"])
