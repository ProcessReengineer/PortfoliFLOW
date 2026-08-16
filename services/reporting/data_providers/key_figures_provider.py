# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for the NAV / IRR / TVPI / DPI key-figures strip.

Returns a :class:`KeyFigures` dataclass instead of a DataFrame because the
result is a fixed-shape collection of scalars rather than a tabular value.

Internally calls the same helpers used by :class:`MultiplesProvider` and
:class:`IRRProvider` so the strip values are guaranteed consistent with the
charts.

Defensive against CF Out sign inconsistencies — see CLAUDE.md Excel import schema
for the canonical convention.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from core.data_store import get_data_store
from services.reporting.data_providers._calculations import (
    compute_irr,
    compute_tvpi_dpi,
    cumulative_calls_magnitude,
    cumulative_distributions,
    latest_nav,
)
from services.reporting.data_providers.base import ProviderContext


@dataclass(frozen=True)
class KeyFigures:
    """Scalar key figures for the per-tile key-figures strip.

    Attributes:
        nav_eur: Absolute NAV in EUR.  ``None`` if no NAV data is available.
        irr: IRR-since-inception as a decimal (e.g. ``0.123`` = 12.3 %).
            ``None`` if the IRR did not converge or there are too few flows.
        tvpi: Total Value to Paid-In multiple.  ``None`` if no capital has
            been called.
        dpi: Distributions to Paid-In multiple.  ``None`` if no capital has
            been called.
    """

    nav_eur: float | None
    irr: float | None
    tvpi: float | None
    dpi: float | None


class KeyFiguresProvider:
    """Compute the four scalar metrics shown in the key-figures strip."""

    def get(self, ctx: ProviderContext) -> KeyFigures:
        """Return :class:`KeyFigures` for the given context.

        Args:
            ctx: Provider context.  ``investment_filter is None`` produces
                portfolio-aggregate key figures; otherwise scoped to a single
                investment.

        Returns:
            :class:`KeyFigures` with all four metrics.  Each metric is
            ``None`` when underlying data is insufficient to compute it.
        """
        store = get_data_store()
        df_in = store.get("cash_flow_in_actual")
        df_out = store.get("cash_flow_out_actual")
        df_nav = store.get("navs_actual")

        if df_in is None or df_out is None or df_nav is None:
            return KeyFigures(nav_eur=None, irr=None, tvpi=None, dpi=None)

        if ctx.investment_filter is None:
            cols = [
                c
                for c in ctx.all_investments
                if c in df_in.columns and c in df_out.columns and c in df_nav.columns
            ]
            if not cols:
                return KeyFigures(nav_eur=None, irr=None, tvpi=None, dpi=None)
            cf_in_series = df_in[cols].sum(axis=1)
            cf_out_series = df_out[cols].sum(axis=1)
            nav_series = df_nav[cols].sum(axis=1, min_count=1)
        else:
            inv = ctx.investment_filter
            cf_in_series = df_in[inv] if inv in df_in.columns else pd.Series(dtype="float64")
            cf_out_series = df_out[inv] if inv in df_out.columns else pd.Series(dtype="float64")
            nav_series = df_nav[inv] if inv in df_nav.columns else pd.Series(dtype="float64")

        nav_v = latest_nav(nav_series, ctx.report_date)
        calls_mag = cumulative_calls_magnitude(cf_out_series, ctx.report_date)
        dist = cumulative_distributions(cf_in_series, ctx.report_date)
        tvpi, dpi = compute_tvpi_dpi(calls_mag, dist, nav_v)
        irr = compute_irr(cf_in_series, cf_out_series, nav_v, ctx.report_date)

        return KeyFigures(
            nav_eur=_optional(nav_v, default_zero_means_none=True),
            irr=_optional(irr),
            tvpi=_optional(tvpi),
            dpi=_optional(dpi),
        )


def _optional(value: float, *, default_zero_means_none: bool = False) -> float | None:
    """Return ``None`` for non-finite (or zero, when requested) inputs.

    Args:
        value: Float to consider.
        default_zero_means_none: When ``True``, treat exact ``0.0`` as
            "no data" and return ``None``.  Used for NAV where zero
            indicates the absence of any value.

    Returns:
        ``None`` if ``value`` is NaN/inf, or zero with the flag enabled.
        Otherwise ``value``.
    """
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    if default_zero_means_none and value == 0.0:
        return None
    return float(value)
