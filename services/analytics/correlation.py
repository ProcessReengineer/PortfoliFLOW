# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pairwise correlation matrix — Implemented in sub-stream 5c.

Pure-Python migration of the correlation logic in
``gui/widgets/statistics_widgets.py::CorrelationMatrixWidget``. The
QT widget invokes ``df[cols].corr(method="pearson")`` on a wide
DataFrame whose columns are investment names; this module is the
calculation half — DB-free, Qt-free — that both the web side
(sub-stream 5c) and the Phase-6 GUI-on-Postgres reorientation
consume.

Convention copied bit-for-bit from the QT side:

- **Method.** Pearson — pandas default, matches ``df.corr(method="pearson")``.
- **Pairwise complete observations.** Pandas's default (``min_periods=1``)
  evaluates each off-diagonal cell on the dates where both columns
  have non-NaN values. The matrix therefore handles unaligned NAV /
  return histories without forcing the caller to align upstream.
- **Diagonal.** Always 1.0 (or NaN if a column is empty / constant —
  pandas's behaviour). The Statistics page styles diagonal cells
  separately, so the calculation is faithful to pandas.
- **Stable column / row order.** The matrix preserves the order of
  the input dict so the chart-spec generator and the QT widget can
  align labels deterministically.
"""

from __future__ import annotations

import pandas as pd


def compute_correlation_matrix(
    return_series_by_investment: dict[str, pd.Series],
) -> pd.DataFrame:
    """Pairwise Pearson correlation across multiple return series.

    Mirrors ``df[cols].corr(method="pearson")`` from the QT widget.
    Series are aligned on their date index; pandas handles
    pairwise-complete observations natively.

    Args:
        return_series_by_investment: Dict mapping investment name to
            its periodic return series. Series may have differing
            indexes; missing dates are treated as NaN under the
            pandas alignment.

    Returns:
        Square :class:`pandas.DataFrame` whose index and columns are
        the investment names in the dict's iteration order. Values
        in ``[-1, 1]`` (NaN where pandas cannot compute the
        coefficient — e.g. a series with no variance). Empty
        DataFrame when the input dict is empty.
    """
    if not return_series_by_investment:
        return pd.DataFrame()

    aligned = pd.DataFrame({name: series for name, series in return_series_by_investment.items()})
    corr = aligned.corr(method="pearson")
    names = list(return_series_by_investment.keys())
    return corr.reindex(index=names, columns=names)
