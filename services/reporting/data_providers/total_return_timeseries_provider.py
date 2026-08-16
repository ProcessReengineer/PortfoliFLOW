# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for the per-investment cumulative total return rebased to 100.

Reads the daily total-return series for a single investment from
``total_return_actual`` and returns a date-indexed DataFrame with one
column ``"rebased"`` whose value at date ``t`` is::

    rebased_t = 100 * cumprod(1 + total_return_actual[inv].dropna()) up to t,
                normalised so the first row equals exactly 100.

Portfolio-level mode is intentionally unsupported — aggregating Total
Return across investments requires NAV-weighted compounding which is not
in scope here.
"""

from __future__ import annotations

import logging

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers.base import DataProvider, ProviderContext

logger = logging.getLogger(__name__)


class TotalReturnTimeseriesProvider(DataProvider):
    """Per-investment cumulative total return rebased to 100 at inception.

    The rebased value at date ``t`` is::

        rebased_t = 100 * cumprod(1 + total_return_actual[inv].dropna()) up to t

    Where the cumprod starts at the first date where ``total_return_actual[inv]``
    has a non-NaN value, and the rebased series begins at exactly ``100`` on
    that date.

    Portfolio-level mode (``investment_filter is None``) is NOT supported and
    returns an empty DataFrame — aggregating Total Return across investments
    requires NAV-weighted compounding which is not in scope here.
    """

    _COLUMNS = ("rebased",)

    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a date-indexed DataFrame with the rebased total-return series.

        Args:
            ctx: Provider context.  ``ctx.investment_filter`` must be set;
                portfolio mode returns an empty DataFrame.

        Returns:
            DataFrame with a single column ``"rebased"`` and a
            :class:`~pandas.DatetimeIndex` named ``"date"``.  The first
            non-NaN row is exactly ``100.0`` by construction.  Empty if any
            of the precondition checks fails (see module docstring).
        """
        empty = pd.DataFrame(columns=list(self._COLUMNS))
        empty.index = pd.DatetimeIndex([], name="date")

        if ctx.investment_filter is None:
            return empty

        store = get_data_store()
        df = store.get("total_return_actual")
        if df is None:
            return empty

        inv = ctx.investment_filter
        if inv not in df.columns:
            return empty

        series = pd.to_numeric(df[inv], errors="coerce").dropna()
        series = series[series.index <= ctx.report_date]
        if series.empty:
            return empty

        first_factor = 1.0 + float(series.iloc[0])
        if first_factor == 0.0:
            # Pathological -100% return on day one: rebased base is undefined.
            return empty

        rebased = (1.0 + series).cumprod() * (100.0 / first_factor)
        result = rebased.to_frame(name="rebased")
        result.index.name = "date"
        return result
