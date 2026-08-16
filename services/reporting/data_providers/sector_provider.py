# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider for the NAV-weighted sector breakdown.

Uses :func:`services.reporting.attributes_partition.partition_attributes` to
identify the rows in the ``attributes`` DataFrame that represent a sector
breakdown, then aggregates them via :func:`compute_breakdown`.

Defensive against CF Out sign inconsistencies — irrelevant here (no
cashflows used), but the docstring note is preserved for consistency with
the other providers.
"""

from __future__ import annotations

import pandas as pd

from core.data_store import get_data_store
from services.reporting.attributes_partition import partition_attributes
from services.reporting.data_providers._breakdown import (
    BREAKDOWN_COLUMNS,
    compute_breakdown,
)
from services.reporting.data_providers.base import DataProvider, ProviderContext


class SectorProvider(DataProvider):
    """NAV-weighted breakdown across sector attribute rows."""

    def get(self, ctx: ProviderContext) -> pd.DataFrame:
        """Return a DataFrame of NAV-weighted sector shares.

        Args:
            ctx: Provider context.

        Returns:
            DataFrame with columns ``["category", "share"]``.  Empty if the
            attributes DataFrame is missing or no sector breakdown rows are
            detected.
        """
        store = get_data_store()
        df_attr = store.get("attributes")
        if df_attr is None:
            return pd.DataFrame(columns=list(BREAKDOWN_COLUMNS))
        partition = partition_attributes(df_attr)
        return compute_breakdown(ctx, partition.sector_rows)
