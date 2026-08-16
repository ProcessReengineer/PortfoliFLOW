# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for the year-end cashflow bars + NAV / NCG line overlay.

Returns a year-end indexed DataFrame with columns
``["calls", "distributions", "nav", "ncg"]``:

* ``calls`` and ``distributions`` are produced by reusing
  :class:`CashflowProvider` (calls negative, distributions positive).
* ``nav`` is the most recent NAV at or before each year-end.  Aggregated
  across investments in portfolio mode; scoped to the single investment in
  per-investment mode.
* ``ncg`` (Net Capital Gain) is
  ``nav + cumulative_distributions - cumulative_calls_magnitude``.

Both portfolio mode (``ctx.investment_filter is None``) and single-investment
mode are supported.
"""

from __future__ import annotations

import logging

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers.base import DataProvider, ProviderContext
from services.reporting.data_providers.cashflow_provider import CashflowProvider

logger = logging.getLogger(__name__)


class CashflowWithNavProvider(DataProvider):
    """Year-end cashflows with NAV and Net-Capital-Gain overlay lines."""

    _COLUMNS = ("calls", "distributions", "nav", "ncg")

    def __init__(self, cashflow_provider: CashflowProvider | None = None) -> None:
        """Construct the provider.

        Args:
            cashflow_provider: Optional injected :class:`CashflowProvider`
                used for the bar columns.  Defaults to a fresh instance.
        """
        self._cashflow = cashflow_provider or CashflowProvider()

    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a year-indexed DataFrame of calls, distributions, NAV, and NCG.

        Args:
            ctx: Provider context.  When ``ctx.investment_filter`` is set,
                the result is scoped to that single investment; otherwise it
                aggregates across ``ctx.all_investments``.

        Returns:
            Year-indexed DataFrame with columns
            ``["calls", "distributions", "nav", "ncg"]``.
        """
        empty = pd.DataFrame(columns=list(self._COLUMNS))
        empty.index = empty.index.astype(int)
        empty.index.name = "year"

        cashflows = self._cashflow.get(ctx)
        if cashflows.empty:
            return empty

        nav_series = self._build_nav_series(ctx)

        # cashflows index is year-end Timestamp; reduce to int years.
        years = [int(idx.year) if hasattr(idx, "year") else int(idx) for idx in cashflows.index]

        calls = cashflows["calls"].astype(float).to_numpy()
        dists = cashflows["distributions"].astype(float).to_numpy()
        cum_calls_mag = (-calls).cumsum()  # calls are negative — magnitude is cumulative
        cum_dist = dists.cumsum()

        navs: list[float] = []
        for year in years:
            year_end = pd.Timestamp(year=year, month=12, day=31)
            nav_window = nav_series.dropna().loc[nav_series.dropna().index <= year_end]
            navs.append(float(nav_window.iloc[-1]) if not nav_window.empty else 0.0)

        nav_arr = pd.Series(navs, dtype="float64").to_numpy()
        ncg_arr = nav_arr + cum_dist - cum_calls_mag

        df = pd.DataFrame(
            {
                "calls": calls,
                "distributions": dists,
                "nav": nav_arr,
                "ncg": ncg_arr,
            },
            index=years,
        )
        df.index = df.index.astype(int)
        df.index.name = "year"
        return df[list(self._COLUMNS)]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_nav_series(self, ctx: ProviderContext) -> pd.Series:
        """Return the NAV series used for the overlay lines.

        Aggregates across all investments in portfolio mode; returns the
        single-column NAV series in per-investment mode.

        Args:
            ctx: Provider context.

        Returns:
            Date-indexed NAV series, possibly empty.
        """
        store = get_data_store()
        df_nav = store.get("navs_actual")
        if df_nav is None:
            return pd.Series(dtype="float64")

        if ctx.investment_filter is None:
            cols_nav = [c for c in ctx.all_investments if c in df_nav.columns]
            nav_series = (
                df_nav[cols_nav].sum(axis=1, min_count=1)
                if cols_nav
                else pd.Series(dtype="float64")
            )
        else:
            inv = ctx.investment_filter
            nav_series = df_nav[inv] if inv in df_nav.columns else pd.Series(dtype="float64")

        return nav_series[nav_series.index <= ctx.report_date]
