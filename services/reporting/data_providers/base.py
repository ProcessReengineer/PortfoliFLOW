# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Abstract base class for report data providers.

A data provider takes a DataStore snapshot via a :class:`ProviderContext` and
returns a normalised DataFrame shaped for direct consumption by a chart
builder.  Providers contain NO matplotlib code and NO Qt code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ProviderContext:
    """Inputs available to every data provider on every call.

    Attributes:
        report_date: The as-of date — last date considered for the report.
        all_investments: Ordered tuple of all investment names in canonical
            (Excel-row-1) order.  Providers may use this for multi-investment
            output even when ``investment_filter`` is None (e.g. multiples
            chart).
        investment_filter: If ``None``, provider returns portfolio-aggregate
            data.  If a single investment name, provider returns data scoped
            to that investment only.
    """

    report_date: pd.Timestamp
    all_investments: tuple[str, ...]
    investment_filter: str | None = None


class DataProvider(ABC):
    """Abstract base for data providers.  Subclasses implement :meth:`get`."""

    @abstractmethod
    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a normalised DataFrame for the chart builder.

        Args:
            ctx: The :class:`ProviderContext` describing report scope.

        Returns:
            A DataFrame whose schema is documented per-subclass.  Returns an
            empty DataFrame (with the expected columns) if data is unavailable
            — never raises for missing data.
        """
