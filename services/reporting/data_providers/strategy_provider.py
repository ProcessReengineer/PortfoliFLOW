# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for the NAV-weighted strategy split.

Reads the ``Investment Sub-Class`` row from the ``attributes`` DataFrame and
groups the latest NAV per investment by sub-class.  Returns a 2-column
DataFrame ``["sub_class", "nav_share"]`` whose ``nav_share`` sums to ``1.0``.

Defensive against CF Out sign inconsistencies — irrelevant here (no
cashflows used), but the docstring note is preserved for consistency with
the other providers.
"""

from __future__ import annotations

import logging

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers._calculations import latest_nav
from services.reporting.data_providers.base import DataProvider, ProviderContext

logger = logging.getLogger(__name__)

_PLACEHOLDER_SUBCLASS: frozenset[str] = frozenset({"Klasse der Investition"})


class StrategyProvider(DataProvider):
    """NAV-weighted breakdown by ``Investment Sub-Class``."""

    _COLUMNS = ("sub_class", "nav_share")

    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a DataFrame of NAV-weighted shares per sub-class.

        At portfolio level, sub-classes are aggregated across all investments
        and normalised to ``1.0``.  At single-investment level the result is a
        single row with ``nav_share = 1.0``.

        Args:
            ctx: Provider context.

        Returns:
            DataFrame with columns ``["sub_class", "nav_share"]``.  Empty if
            the attributes or NAV datasets are missing or no valid sub-class
            value can be extracted.
        """
        store = get_data_store()
        df_attr = store.get("attributes")
        df_nav = store.get("navs_actual")
        empty = pd.DataFrame(columns=list(self._COLUMNS))

        if df_attr is None or "Investment Sub-Class" not in df_attr.index:
            return empty

        sub_class_row = df_attr.loc["Investment Sub-Class"]

        if ctx.investment_filter is not None:
            inv = ctx.investment_filter
            if inv not in sub_class_row.index:
                return empty
            value = sub_class_row[inv]
            if not _is_valid_subclass(value):
                return empty
            return pd.DataFrame(
                [(str(value), 1.0)],
                columns=list(self._COLUMNS),
            )

        if df_nav is None:
            return empty

        rows: list[tuple[str, float]] = []
        for inv in ctx.all_investments:
            if inv not in sub_class_row.index:
                continue
            value = sub_class_row[inv]
            if not _is_valid_subclass(value):
                continue
            nav_inv = df_nav[inv] if inv in df_nav.columns else pd.Series(dtype="float64")
            nav_v = latest_nav(nav_inv, ctx.report_date)
            if nav_v <= 0.0:
                continue
            rows.append((str(value), nav_v))

        if not rows:
            return empty

        per_inv = pd.DataFrame(rows, columns=["sub_class", "nav"])
        agg = per_inv.groupby("sub_class", sort=False)["nav"].sum()
        total = float(agg.sum())
        if total <= 0.0:
            return empty
        result = pd.DataFrame(
            {
                "sub_class": agg.index.tolist(),
                "nav_share": (agg / total).tolist(),
            }
        )
        return result


def _is_valid_subclass(value: object) -> bool:
    """Return ``True`` if ``value`` is a usable sub-class label."""
    if value is None:
        return False
    if isinstance(value, float) and pd.isna(value):
        return False
    text = str(value).strip()
    if not text:
        return False
    if text in _PLACEHOLDER_SUBCLASS:
        return False
    return True
