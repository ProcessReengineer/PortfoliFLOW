# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for the year-end DPI / RVPI / TVPI / IRR time series.

Returns a year-end indexed DataFrame with columns
``["dpi", "rvpi", "tvpi", "irr"]``.  At each year-end ``t``::

    cum_calls_t        = abs(cum cf_out up to t)
    cum_distributions_t = (cum cf_in up to t)
    nav_t              = latest NAV at or before t

    dpi_t  = cum_distributions_t / cum_calls_t   (NaN if cum_calls_t == 0)
    rvpi_t = nav_t / cum_calls_t                 (NaN if cum_calls_t == 0)
    tvpi_t = dpi_t + rvpi_t                      (NaN-propagating)

    irr_t  = IRR of the combined cashflow stream up to t with +nav_t
             appended at t (Brent on [-0.99, 10.0]).  NaN if no convergence
             or fewer than two cashflows.

Both portfolio mode (``ctx.investment_filter is None``) and single-investment
mode are supported.  IRR root finding runs once per year — typically 8–15
iterations — which is acceptable.  No memoisation.
"""

from __future__ import annotations

import logging

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers._calculations import (
    compute_irr,
    cumulative_calls_magnitude,
    cumulative_distributions,
    latest_nav,
)
from services.reporting.data_providers.base import DataProvider, ProviderContext

logger = logging.getLogger(__name__)


class MultiplesTimeseriesProvider(DataProvider):
    """Yearly DPI / RVPI / TVPI / IRR time series.

    Portfolio mode aggregates cashflows and NAV across all investments.
    Per-investment mode scopes the calculation to a single investment.
    """

    _COLUMNS = ("dpi", "rvpi", "tvpi", "irr")

    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a year-indexed DataFrame of DPI, RVPI, TVPI, and IRR.

        Args:
            ctx: Provider context.  Aggregates across ``ctx.all_investments``
                when ``ctx.investment_filter`` is ``None``; otherwise scopes
                to the single named investment.

        Returns:
            Year-indexed DataFrame with columns
            ``["dpi", "rvpi", "tvpi", "irr"]``.
        """
        empty = pd.DataFrame(columns=list(self._COLUMNS))
        empty.index = empty.index.astype(int)
        empty.index.name = "year"

        store = get_data_store()
        df_in = store.get("cash_flow_in_actual")
        df_out = store.get("cash_flow_out_actual")
        df_nav = store.get("navs_actual")
        if df_in is None or df_out is None or df_nav is None:
            return empty

        cf_in_total, cf_out_total, nav_total = self._scope_series(df_in, df_out, df_nav, ctx)
        if cf_out_total.empty and (cf_in_total.empty or cf_in_total.eq(0.0).all()):
            # No cashflow data at all for this scope → no rows.
            return empty

        years = self._year_range(cf_out_total, ctx.report_date)
        if not years:
            return empty

        rows: list[tuple[float, float, float, float]] = []
        for year in years:
            year_end = pd.Timestamp(year=year, month=12, day=31)
            calls_mag = cumulative_calls_magnitude(cf_out_total, year_end)
            dist = cumulative_distributions(cf_in_total, year_end)
            nav_v = latest_nav(nav_total, year_end)

            if calls_mag <= 0.0:
                rows.append((float("nan"), float("nan"), float("nan"), float("nan")))
                continue

            dpi = dist / calls_mag
            rvpi = nav_v / calls_mag
            tvpi = dpi + rvpi
            irr = compute_irr(cf_in_total, cf_out_total, nav_v, year_end)
            rows.append((dpi, rvpi, tvpi, irr))

        df = pd.DataFrame(rows, index=list(years), columns=list(self._COLUMNS))
        df.index = df.index.astype(int)
        df.index.name = "year"
        return df

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _scope_series(
        df_in: pd.DataFrame,
        df_out: pd.DataFrame,
        df_nav: pd.DataFrame,
        ctx: ProviderContext,
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Return ``(cf_in, cf_out, nav)`` series for the requested scope."""
        if ctx.investment_filter is None:
            cols_in = [c for c in ctx.all_investments if c in df_in.columns]
            cols_out = [c for c in ctx.all_investments if c in df_out.columns]
            cols_nav = [c for c in ctx.all_investments if c in df_nav.columns]
            cf_in_total = df_in[cols_in].sum(axis=1) if cols_in else pd.Series(dtype="float64")
            cf_out_total = df_out[cols_out].sum(axis=1) if cols_out else pd.Series(dtype="float64")
            nav_total = (
                df_nav[cols_nav].sum(axis=1, min_count=1)
                if cols_nav
                else pd.Series(dtype="float64")
            )
        else:
            inv = ctx.investment_filter
            cf_in_total = df_in[inv] if inv in df_in.columns else pd.Series(dtype="float64")
            cf_out_total = df_out[inv] if inv in df_out.columns else pd.Series(dtype="float64")
            nav_total = df_nav[inv] if inv in df_nav.columns else pd.Series(dtype="float64")
        return cf_in_total, cf_out_total, nav_total

    @staticmethod
    def _year_range(cf_out_total: pd.Series, report_date: pd.Timestamp) -> list[int]:
        """Year range from the earliest non-zero cashflow to the report year."""
        if cf_out_total is None or cf_out_total.empty:
            return []
        bounded = cf_out_total[cf_out_total.index <= report_date].fillna(0.0)
        non_zero = bounded[bounded != 0.0]
        if non_zero.empty:
            return []
        first_year = int(non_zero.index.min().year)
        last_year = int(report_date.year)
        if last_year < first_year:
            return []
        return list(range(first_year, last_year + 1))
