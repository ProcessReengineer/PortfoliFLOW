# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service layer for the universe-wide statistics surface (sub-stream 5c).

Aggregates the Phase-4 NAV repository into the
:class:`UniverseStatisticsBundle` consumed by the ``/statistics``
web route and its chart-spec generators. The orchestration lives
in this package so the web route stays a thin transport seam and
the analytics layer stays DB-free.
"""

from services.statistics.statistics_service import (
    StatisticsService,
    UniverseStatisticsBundle,
)

__all__ = [
    "StatisticsService",
    "UniverseStatisticsBundle",
]
