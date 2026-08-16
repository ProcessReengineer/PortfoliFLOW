# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for the year-end Invested-Capital + NAV time series.

Returns a year-end indexed DataFrame with columns
``["invested_capital", "nav"]`` — both in EUR.

* ``invested_capital`` is the magnitude of cumulative capital calls summed
  across all investments (portfolio mode) or for a single investment
  (per-investment mode), up to and including each year-end.
* ``nav`` is the (cross-investment sum or single-investment) most recent
  non-NaN NAV value at or before each year-end.

Both portfolio mode (``ctx.investment_filter is None``) and single-investment
mode are supported.
"""

from __future__ import annotations

import logging

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers.base import DataProvider, ProviderContext

logger = logging.getLogger(__name__)


class InvestedNavProvider(DataProvider):
    """Yearly invested capital and NAV totals.

    Portfolio mode aggregates across all investments listed in
    ``ctx.all_investments``.  Per-investment mode scopes the calculation to
    ``ctx.investment_filter``.
    """

    _COLUMNS = ("invested_capital", "nav")

    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a year-indexed DataFrame of invested capital and NAV.

        Args:
            ctx: Provider context.  Aggregates across ``ctx.all_investments``
                when ``ctx.investment_filter`` is ``None``; otherwise scopes
                to the single named investment.

        Returns:
            Year-indexed DataFrame with columns
            ``["invested_capital", "nav"]``.  Both columns are in EUR and
            non-negative.  Empty if the DataStore lacks the required
            datasets, or (per-investment mode) if the investment is missing
            from both source DataFrames.
        """
        empty = pd.DataFrame(columns=list(self._COLUMNS))
        empty.index = empty.index.astype(int)
        empty.index.name = "year"

        store = get_data_store()
        df_out = store.get("cash_flow_out_actual")
        df_nav = store.get("navs_actual")
        if df_out is None or df_nav is None:
            return empty

        if ctx.investment_filter is None:
            cf_out_total, nav_total = self._portfolio_series(df_out, df_nav, ctx)
        else:
            inv = ctx.investment_filter
            if inv not in df_out.columns and inv not in df_nav.columns:
                return empty
            cf_out_total, nav_total = self._single_investment_series(df_out, df_nav, inv)

        cf_out_total = cf_out_total[cf_out_total.index <= ctx.report_date].fillna(0.0)
        nav_total = nav_total[nav_total.index <= ctx.report_date]

        # NAV-only fallback only applies in per-investment mode (an early-stage
        # investment may have a NAV before its first capital call).  Portfolio
        # mode keeps its original behaviour: if no calls exist, no rows.
        nav_for_range = nav_total if ctx.investment_filter is not None else None
        years = self._year_range(cf_out_total, nav_for_range, ctx.report_date)
        if not years:
            return empty

        cum_calls = cf_out_total.cumsum() if not cf_out_total.empty else cf_out_total
        nav_clean = nav_total.dropna()

        rows: list[tuple[float, float]] = []
        for year in years:
            year_end = pd.Timestamp(year=year, month=12, day=31)
            if not cum_calls.empty:
                window = cum_calls.loc[cum_calls.index <= year_end]
                invested = float(window.iloc[-1]) if not window.empty else 0.0
            else:
                invested = 0.0
            nav_window = nav_clean.loc[nav_clean.index <= year_end]
            nav_v = float(nav_window.iloc[-1]) if not nav_window.empty else 0.0
            rows.append((invested, nav_v))

        df = pd.DataFrame(rows, index=list(years), columns=list(self._COLUMNS))
        df.index = df.index.astype(int)
        df.index.name = "year"
        return df

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _portfolio_series(
        df_out: pd.DataFrame,
        df_nav: pd.DataFrame,
        ctx: ProviderContext,
    ) -> tuple[pd.Series, pd.Series]:
        """Return ``(cf_out_magnitude_total, nav_total)`` aggregated across investments."""
        cols_out = [c for c in ctx.all_investments if c in df_out.columns]
        cols_nav = [c for c in ctx.all_investments if c in df_nav.columns]
        cf_out = df_out[cols_out].sum(axis=1).abs() if cols_out else pd.Series(dtype="float64")
        nav = df_nav[cols_nav].sum(axis=1, min_count=1) if cols_nav else pd.Series(dtype="float64")
        return cf_out, nav

    @staticmethod
    def _single_investment_series(
        df_out: pd.DataFrame,
        df_nav: pd.DataFrame,
        inv: str,
    ) -> tuple[pd.Series, pd.Series]:
        """Return ``(cf_out_magnitude, nav)`` for a single investment column."""
        cf_out = df_out[inv].abs() if inv in df_out.columns else pd.Series(dtype="float64")
        nav = df_nav[inv] if inv in df_nav.columns else pd.Series(dtype="float64")
        return cf_out, nav

    @staticmethod
    def _year_range(
        cf_out: pd.Series,
        nav: pd.Series | None,
        report_date: pd.Timestamp,
    ) -> list[int]:
        """Compute the year range from the earliest activity to the report year.

        The earliest year is the first year carrying a non-zero call.  If
        ``nav`` is provided (per-investment mode) and no calls exist, the
        earliest year falls back to the first year carrying a non-NaN NAV.

        Args:
            cf_out: Magnitude of capital calls (date-indexed).
            nav: Optional NAV series — used as a fallback only.  Pass
                ``None`` to disable the fallback (portfolio mode).
            report_date: The as-of date.

        Returns:
            List of integer years (inclusive endpoints), or empty if no
            calls (and no NAV fallback) exist.
        """
        first_year: int | None = None
        non_zero = cf_out[cf_out > 0.0] if not cf_out.empty else cf_out
        if not non_zero.empty:
            first_year = int(non_zero.index.min().year)

        if first_year is None and nav is not None:
            nav_clean = nav.dropna()
            if not nav_clean.empty:
                first_year = int(nav_clean.index.min().year)

        if first_year is None:
            return []
        last_year = int(report_date.year)
        if last_year < first_year:
            return []
        return list(range(first_year, last_year + 1))
