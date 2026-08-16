# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for the per-vintage NAV-share and investment-count distribution.

Returns a DataFrame indexed by vintage year (``int``, sorted ascending) with
columns ``["nav_share", "investment_count"]``:

* ``nav_share[year]`` = sum of latest NAV across investments with
  ``Vintage Year == year`` divided by total portfolio NAV.  Sums to ``1.0``
  across all rows.
* ``investment_count[year]`` = number of investments with that vintage.

Portfolio-only.  Reads ``Vintage Year`` from the ``attributes`` DataFrame.
Returns an empty DataFrame if the row is missing or no usable vintages can be
derived.
"""

from __future__ import annotations

import logging

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers._calculations import latest_nav
from services.reporting.data_providers.base import DataProvider, ProviderContext

logger = logging.getLogger(__name__)


class VintagesProvider(DataProvider):
    """NAV-weighted vintage distribution for the portfolio overview."""

    _COLUMNS = ("nav_share", "investment_count")

    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a vintage-year-indexed DataFrame of NAV shares and counts.

        Args:
            ctx: Provider context.  Returns an empty DataFrame when
                ``ctx.investment_filter`` is set (portfolio-only).

        Returns:
            Integer-indexed DataFrame with columns
            ``["nav_share", "investment_count"]``.
        """
        empty = pd.DataFrame(columns=list(self._COLUMNS))
        empty.index = empty.index.astype(int)
        empty.index.name = "vintage"
        if ctx.investment_filter is not None:
            return empty

        store = get_data_store()
        df_attr = store.get("attributes")
        df_nav = store.get("navs_actual")
        if df_attr is None or df_nav is None:
            return empty
        if "Vintage Year" not in df_attr.index:
            return empty

        vintage_row = df_attr.loc["Vintage Year"]
        per_inv: dict[str, tuple[int, float]] = {}
        for inv in ctx.all_investments:
            if inv not in vintage_row.index:
                continue
            raw = vintage_row[inv]
            year = self._coerce_year(raw)
            if year is None:
                continue
            nav_series = df_nav[inv] if inv in df_nav.columns else pd.Series(dtype="float64")
            nav_v = latest_nav(nav_series, ctx.report_date)
            if nav_v < 0.0:
                continue
            per_inv[inv] = (year, nav_v)

        if not per_inv:
            return empty

        total_nav = sum(nav for _, nav in per_inv.values())
        if total_nav <= 0.0:
            return empty

        # Aggregate by vintage year.
        by_year: dict[int, list[float]] = {}
        for _, (year, nav_v) in per_inv.items():
            by_year.setdefault(year, []).append(nav_v)

        years_sorted = sorted(by_year.keys())
        rows = [(sum(by_year[y]) / total_nav, len(by_year[y])) for y in years_sorted]

        df = pd.DataFrame(rows, index=years_sorted, columns=list(self._COLUMNS))
        df["investment_count"] = df["investment_count"].astype(int)
        df.index = df.index.astype(int)
        df.index.name = "vintage"
        return df

    def _coerce_year(self, raw: object) -> int | None:
        """Coerce a raw cell value to an integer year, or ``None`` if invalid."""
        if raw is None:
            return None
        if isinstance(raw, float) and pd.isna(raw):
            return None
        try:
            return int(float(str(raw).strip()))
        except (ValueError, TypeError):
            return None
