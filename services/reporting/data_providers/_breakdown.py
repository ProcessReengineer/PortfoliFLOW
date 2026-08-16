# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared breakdown logic for the country and sector providers.

Both providers consume the same data — the ``attributes`` DataFrame
partitioned via :mod:`services.reporting.attributes_partition` — and apply
identical aggregation rules.  Only the row subset (sectors vs. countries)
differs.

Defensive against CF Out sign inconsistencies — irrelevant here (no
cashflows used), but the docstring note is preserved for consistency with
the other providers.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers._calculations import latest_nav
from services.reporting.data_providers.base import ProviderContext

BREAKDOWN_COLUMNS = ("category", "share")


def compute_breakdown(
    ctx: ProviderContext,
    row_labels: Iterable[str],
) -> pd.DataFrame:
    """Compute a 2-column breakdown DataFrame from a list of attribute rows.

    At portfolio level (``ctx.investment_filter is None``) each row's share is
    a NAV-weighted aggregate across all investments::

        share[row] = sum_inv(weight[row, inv] * latest_nav[inv]) / sum_inv(latest_nav[inv])

    At single-investment level the share is the raw weight column for that
    investment.  Rows whose share is zero are dropped.

    Args:
        ctx: Provider context.
        row_labels: Index labels of the attribute rows to aggregate.

    Returns:
        DataFrame with columns ``["category", "share"]``.  Empty if no
        attribute rows are supplied or if no investment carries a non-zero
        weight.
    """
    labels = tuple(row_labels)
    empty = pd.DataFrame(columns=list(BREAKDOWN_COLUMNS))
    if not labels:
        return empty

    store = get_data_store()
    df_attr = store.get("attributes")
    if df_attr is None:
        return empty

    valid_labels = [lbl for lbl in labels if lbl in df_attr.index]
    if not valid_labels:
        return empty

    weights = df_attr.loc[valid_labels].apply(pd.to_numeric, errors="coerce").fillna(0.0)

    if ctx.investment_filter is not None:
        inv = ctx.investment_filter
        if inv not in weights.columns:
            return empty
        shares = weights[inv]
    else:
        df_nav = store.get("navs_actual")
        if df_nav is None:
            return empty
        nav_per_inv: dict[str, float] = {}
        for inv in ctx.all_investments:
            if inv not in weights.columns:
                continue
            nav_series = df_nav[inv] if inv in df_nav.columns else pd.Series(dtype="float64")
            nav_v = latest_nav(nav_series, ctx.report_date)
            if nav_v > 0.0:
                nav_per_inv[inv] = nav_v
        if not nav_per_inv:
            return empty
        nav_vec = pd.Series(nav_per_inv, dtype="float64")
        usable_invs = [c for c in nav_vec.index if c in weights.columns]
        if not usable_invs:
            return empty
        weighted = weights[usable_invs].mul(nav_vec[usable_invs], axis=1).sum(axis=1)
        total_nav = float(nav_vec[usable_invs].sum())
        if total_nav <= 0.0:
            return empty
        shares = weighted / total_nav

    nonzero = shares[shares > 0.0]
    if nonzero.empty:
        return empty

    return pd.DataFrame(
        {
            "category": [str(lbl) for lbl in nonzero.index],
            "share": nonzero.tolist(),
        }
    )
