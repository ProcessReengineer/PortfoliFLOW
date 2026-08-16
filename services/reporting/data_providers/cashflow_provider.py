# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for year-aggregated cash flow bars.

Returns a DataFrame indexed by year-end timestamp with columns
``["calls", "distributions"]``.  ``calls`` are negative (capital invested by
LP, sign-normalised).  ``distributions`` are positive.

Defensive against CF Out sign inconsistencies — see CLAUDE.md Excel import schema
for the canonical convention.  This provider always emits ``calls`` as
negative values regardless of the input sign.
"""

from __future__ import annotations

import logging

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers.base import DataProvider, ProviderContext

logger = logging.getLogger(__name__)


class CashflowProvider(DataProvider):
    """Yearly cashflow aggregation for the cashflow-by-year chart."""

    _COLUMNS = ("calls", "distributions")

    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a year-indexed DataFrame of calls and distributions.

        Aggregated across all investments if ``ctx.investment_filter`` is
        ``None``, otherwise scoped to that single investment.

        Args:
            ctx: Provider context.

        Returns:
            DataFrame indexed by year-end timestamp with columns
            ``["calls", "distributions"]``.  Empty (zero-row) DataFrame if the
            DataStore lacks the required cashflow datasets.
        """
        store = get_data_store()
        df_in = store.get("cash_flow_in_actual")
        df_out = store.get("cash_flow_out_actual")
        empty = pd.DataFrame(columns=list(self._COLUMNS))
        if df_in is None or df_out is None:
            return empty

        if ctx.investment_filter is None:
            cols = [c for c in df_in.columns if c in df_out.columns]
            in_total = df_in[cols].sum(axis=1)
            out_total = df_out[cols].sum(axis=1).abs() * -1.0
        else:
            inv = ctx.investment_filter
            if inv in df_in.columns:
                in_total = df_in[inv]
            else:
                in_total = pd.Series(dtype="float64")
            if inv in df_out.columns:
                out_total = df_out[inv].abs() * -1.0
            else:
                out_total = pd.Series(dtype="float64")

        in_total = in_total[in_total.index <= ctx.report_date].fillna(0.0)
        out_total = out_total[out_total.index <= ctx.report_date].fillna(0.0)

        if in_total.empty and out_total.empty:
            return empty

        yearly = pd.concat(
            {
                "distributions": in_total.resample("YE").sum(),
                "calls": out_total.resample("YE").sum(),
            },
            axis=1,
            sort=True,
        ).fillna(0.0)
        return yearly[list(self._COLUMNS)]
