# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Data providers for the Portfolio Review report.

Each provider takes a :class:`ProviderContext` and returns a normalised
DataFrame (or, in the case of :class:`KeyFiguresProvider`, a fixed-shape
:class:`KeyFigures` dataclass) ready for direct consumption by a chart builder.

Providers contain no matplotlib code and no Qt code; they read only from the
DataStore singleton.
"""

from services.reporting.data_providers.base import DataProvider, ProviderContext
from services.reporting.data_providers.cashflow_provider import CashflowProvider
from services.reporting.data_providers.cashflow_with_nav_provider import (
    CashflowWithNavProvider,
)
from services.reporting.data_providers.country_provider import CountryProvider
from services.reporting.data_providers.invested_nav_provider import (
    InvestedNavProvider,
)
from services.reporting.data_providers.irr_provider import IRRProvider
from services.reporting.data_providers.key_figures_provider import (
    KeyFigures,
    KeyFiguresProvider,
)
from services.reporting.data_providers.multiples_provider import MultiplesProvider
from services.reporting.data_providers.multiples_timeseries_provider import (
    MultiplesTimeseriesProvider,
)
from services.reporting.data_providers.sector_provider import SectorProvider
from services.reporting.data_providers.strategy_provider import StrategyProvider
from services.reporting.data_providers.total_return_timeseries_provider import (
    TotalReturnTimeseriesProvider,
)
from services.reporting.data_providers.vintages_provider import VintagesProvider

__all__ = [
    "DataProvider",
    "ProviderContext",
    "CashflowProvider",
    "CashflowWithNavProvider",
    "InvestedNavProvider",
    "MultiplesProvider",
    "MultiplesTimeseriesProvider",
    "IRRProvider",
    "StrategyProvider",
    "CountryProvider",
    "SectorProvider",
    "TotalReturnTimeseriesProvider",
    "VintagesProvider",
    "KeyFigures",
    "KeyFiguresProvider",
]
