# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for per-investment TVPI and DPI multiples.

Returns a DataFrame indexed by investment name with columns ``["TVPI", "DPI"]``.

Defensive against CF Out sign inconsistencies — see CLAUDE.md Excel import schema
for the canonical convention.  The denominator is always
``abs(cum_cf_out)``.
"""

from __future__ import annotations

import logging

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers._calculations import (
    compute_tvpi_dpi,
    cumulative_calls_magnitude,
    cumulative_distributions,
    latest_nav,
)
from services.reporting.data_providers.base import DataProvider, ProviderContext

logger = logging.getLogger(__name__)


class MultiplesProvider(DataProvider):
    """Per-investment TVPI / DPI as of the report date."""

    _COLUMNS = ("TVPI", "DPI")

    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a DataFrame of TVPI and DPI per investment.

        Args:
            ctx: Provider context.  ``investment_filter`` selects either a
                single investment (1-row result) or all investments
                (one row each in canonical order).

        Returns:
            DataFrame indexed by investment name with columns
            ``["TVPI", "DPI"]``.  Rows for investments without any capital
            called yield NaN values.
        """
        store = get_data_store()
        df_in = store.get("cash_flow_in_actual")
        df_out = store.get("cash_flow_out_actual")
        df_nav = store.get("navs_actual")

        if ctx.investment_filter is not None:
            target_investments: tuple[str, ...] = (ctx.investment_filter,)
        else:
            target_investments = ctx.all_investments

        if df_in is None or df_out is None or df_nav is None:
            return pd.DataFrame(
                index=list(target_investments),
                columns=list(self._COLUMNS),
                dtype="float64",
            )

        rows: list[tuple[float, float]] = []
        for inv in target_investments:
            cf_out_inv = df_out[inv] if inv in df_out.columns else pd.Series(dtype="float64")
            cf_in_inv = df_in[inv] if inv in df_in.columns else pd.Series(dtype="float64")
            nav_inv = df_nav[inv] if inv in df_nav.columns else pd.Series(dtype="float64")

            calls_mag = cumulative_calls_magnitude(cf_out_inv, ctx.report_date)
            dist = cumulative_distributions(cf_in_inv, ctx.report_date)
            nav_v = latest_nav(nav_inv, ctx.report_date)
            tvpi, dpi = compute_tvpi_dpi(calls_mag, dist, nav_v)
            rows.append((tvpi, dpi))

        return pd.DataFrame(
            rows,
            index=list(target_investments),
            columns=list(self._COLUMNS),
            dtype="float64",
        )
