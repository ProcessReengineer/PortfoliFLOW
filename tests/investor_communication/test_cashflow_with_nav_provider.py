# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.data_providers.CashflowWithNavProvider`."""

from __future__ import annotations

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers import (
    CashflowWithNavProvider,
    ProviderContext,
)


def test_columns_for_per_investment_filter(
    populated_store_canonical: None,  # noqa: ARG001
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> None:
    """Per-investment filter still returns the canonical 4-column schema."""
    ctx = ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter="Investition A",
    )
    df = CashflowWithNavProvider().get(ctx)
    assert list(df.columns) == ["calls", "distributions", "nav", "ncg"]
    # And it produces at least one row for an active investment.
    assert not df.empty


def test_ncg_definition_holds_for_known_fixture(
    clean_store: None,  # noqa: ARG001
) -> None:
    """``ncg = nav + cum_dist - cum_calls_magnitude`` for at least 3 year-ends."""
    invs = ("A",)
    cf_in = pd.DataFrame(
        {"A": [0.0, 50.0, 30.0]},
        index=[
            pd.Timestamp("2022-06-30"),
            pd.Timestamp("2023-06-30"),
            pd.Timestamp("2024-06-30"),
        ],
    )
    cf_out = pd.DataFrame(
        {"A": [-100.0, -40.0, 0.0]},
        index=cf_in.index,
    )
    navs = pd.DataFrame(
        {"A": [120.0, 110.0, 130.0]},
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
        all_investments=invs,
        investment_filter=None,
    )
    df = CashflowWithNavProvider().get(ctx)
    assert df.index.tolist() == [2022, 2023, 2024]

    # 2022: cum_calls=100, cum_dist=0, NAV=120 → ncg = 120 + 0 - 100 = 20
    assert df.loc[2022, "ncg"] == 20.0
    # 2023: cum_calls=140, cum_dist=50, NAV=110 → ncg = 110 + 50 - 140 = 20
    assert df.loc[2023, "ncg"] == 20.0
    # 2024: cum_calls=140, cum_dist=80, NAV=130 → ncg = 130 + 80 - 140 = 70
    assert df.loc[2024, "ncg"] == 70.0

    # Calls remain negative; distributions positive.
    assert df.loc[2022, "calls"] == -100.0
    assert df.loc[2023, "distributions"] == 50.0
