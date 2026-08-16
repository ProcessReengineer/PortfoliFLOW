# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.data_providers.IRRProvider`."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from core.data_store import get_data_store
from services.reporting.data_providers import IRRProvider, ProviderContext


def _seed_simple_irr(call: float, distribution: float, nav: float) -> ProviderContext:
    """Seed the DataStore with a one-investment cashflow stream and return its ctx.

    The stream is: ``-call`` on 2023-01-01, ``+distribution`` on 2024-01-01,
    and ``+nav`` as the terminal value at the report date 2024-01-01.

    Args:
        call: Capital call magnitude (positive number).
        distribution: Distribution received exactly one year later.
        nav: Terminal NAV used as the synthetic last cashflow.

    Returns:
        A populated :class:`ProviderContext` ready for the provider.
    """
    store = get_data_store()
    store.clear()
    inv = ("Solo",)
    store.store(
        "attributes",
        pd.DataFrame({"Solo": ["Buyout"]}, index=["Investment Sub-Class"]),
    )
    store.store(
        "cash_flow_in_actual",
        pd.DataFrame({"Solo": [distribution]}, index=[pd.Timestamp("2024-01-01")]),
    )
    store.store(
        "cash_flow_out_actual",
        pd.DataFrame({"Solo": [-call]}, index=[pd.Timestamp("2023-01-01")]),
    )
    store.store(
        "navs_actual",
        pd.DataFrame({"Solo": [nav]}, index=[pd.Timestamp("2024-01-01")]),
    )
    return ProviderContext(
        report_date=pd.Timestamp("2024-01-01"),
        all_investments=inv,
        investment_filter=None,
    )


def test_known_irr_one_year() -> None:
    """A -100 call followed by a +110 distribution one year later → IRR ≈ 10 %."""
    ctx = _seed_simple_irr(call=100.0, distribution=110.0, nav=0.0)
    df = IRRProvider().get(ctx)
    assert df.loc["Solo", "IRR"] == pytest.approx(0.10, abs=1e-3)


def test_irr_with_terminal_nav() -> None:
    """A -100 call now and +110 NAV one year later (no distribution) → IRR ≈ 10 %."""
    ctx = _seed_simple_irr(call=100.0, distribution=0.0, nav=110.0)
    df = IRRProvider().get(ctx)
    assert df.loc["Solo", "IRR"] == pytest.approx(0.10, abs=1e-3)


def test_no_convergence_returns_nan() -> None:
    """All-positive flows have no IRR root and the provider returns NaN."""
    store = get_data_store()
    store.clear()
    store.store(
        "attributes",
        pd.DataFrame({"Solo": ["Buyout"]}, index=["Investment Sub-Class"]),
    )
    store.store(
        "cash_flow_in_actual",
        pd.DataFrame(
            {"Solo": [100.0, 50.0]},
            index=[pd.Timestamp("2023-01-01"), pd.Timestamp("2024-01-01")],
        ),
    )
    store.store(
        "cash_flow_out_actual",
        pd.DataFrame({"Solo": [0.0]}, index=[pd.Timestamp("2023-01-01")]),
    )
    store.store(
        "navs_actual",
        pd.DataFrame({"Solo": [10.0]}, index=[pd.Timestamp("2024-01-01")]),
    )
    ctx = ProviderContext(
        report_date=pd.Timestamp("2024-01-01"),
        all_investments=("Solo",),
        investment_filter=None,
    )
    df = IRRProvider().get(ctx)
    assert math.isnan(df.loc["Solo", "IRR"])
    store.clear()


def test_index_order_preserved(
    populated_store_canonical: None,  # noqa: ARG001
    basic_ctx: ProviderContext,
) -> None:
    """Result preserves canonical investment order."""
    df = IRRProvider().get(basic_ctx)
    assert tuple(df.index) == basic_ctx.all_investments
