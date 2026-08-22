# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the refresh core's kind split (ADR-0125 §4).

Pure-function coverage of ``services.investments.live_refresh._kinds_for_run``
— no DB, no network, no adapter. The helper decides *which* kinds one run
fetches: the price kinds on every run, the full ingestable set only on the
first run of each UTC calendar day.

Coverage:

* the never-run tenant and the first run of a UTC day fetch every ingestable
  kind; a later run of the same day fetches the price kinds only;
* the midnight-UTC boundary reopens the daily kinds;
* the comparison is on **UTC** dates, not on whatever zone the caller's
  ``last_run_at`` happens to carry (the zone trap);
* the price kinds are a strict subset of the ingestable kinds;
* clock skew (``last_run_at`` after ``now``) stays on the price-only branch —
  the helper makes no claim beyond the date comparison.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from services.investments.live_refresh import (
    _INGESTABLE_KINDS,
    _PRICE_KINDS,
    _kinds_for_run,
)
from services.market_data.dto import SeriesKind

#: The reference instant for every case: a mid-day UTC run.
_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)


def test_never_run_tenant_fetches_every_ingestable_kind() -> None:
    """ADR-0125 §4: ``last_run_at is None`` ⇒ the full ingestable set.

    A tenant that has never refreshed has no daily kinds on record, so its
    first run must not be narrowed to prices.
    """
    assert _kinds_for_run(_NOW, None) == _INGESTABLE_KINDS


def test_first_run_of_the_utc_day_fetches_every_ingestable_kind() -> None:
    """ADR-0125 §4: ``last_run_at.date() < now.date()`` in UTC ⇒ all kinds."""
    last_run = datetime(2026, 7, 6, 17, 45, tzinfo=timezone.utc)
    assert _kinds_for_run(_NOW, last_run) == _INGESTABLE_KINDS


def test_later_run_of_the_same_utc_day_fetches_price_kinds_only() -> None:
    """ADR-0125 §4: an intraday run re-fetches the price kinds and nothing else.

    This is the cost rule the sub-hourly cadence rests on — at 15 minutes the
    dividend fetch would otherwise repeat 96 times a day for no new data.
    """
    last_run = _NOW - timedelta(minutes=15)
    assert _kinds_for_run(_NOW, last_run) == _PRICE_KINDS


def test_midnight_utc_boundary_reopens_the_daily_kinds() -> None:
    """ADR-0125 §4: the boundary is the UTC calendar day, not a 24-hour span.

    Twenty minutes separate the two instants, but they fall on different UTC
    dates, so the run is the first of its day and fetches everything.
    """
    last_run = datetime(2026, 7, 6, 23, 50, tzinfo=timezone.utc)
    now = datetime(2026, 7, 7, 0, 10, tzinfo=timezone.utc)
    assert _kinds_for_run(now, last_run) == _INGESTABLE_KINDS


def test_comparison_is_on_utc_dates_not_the_callers_zone() -> None:
    """ADR-0125 §4: both instants are normalised to UTC before comparison.

    ``last_run_at`` here reads 2026-07-07 in Berlin but is 2026-07-06 23:30 in
    UTC, so the run at 00:10 UTC is the first of the UTC day. Taking ``.date()``
    of the caller's zone would wrongly classify it as intraday and skip the
    daily kinds.
    """
    last_run = datetime(2026, 7, 7, 1, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    now = datetime(2026, 7, 7, 0, 10, tzinfo=timezone.utc)

    assert last_run.astimezone(timezone.utc).date() == last_run.date() - timedelta(days=1)
    assert _kinds_for_run(now, last_run) == _INGESTABLE_KINDS


def test_price_kinds_are_a_strict_subset_of_the_ingestable_kinds() -> None:
    """ADR-0125 §4: the price set is carved out of the ingestable set.

    Pins the drift guard the module asserts at import time: every price kind
    can be ingested, and at least one kind is left over as a *daily* kind (so
    the split is a real narrowing, not a rename of the whole set).
    """
    assert SeriesKind.NAV_PRICE in _PRICE_KINDS
    assert set(_PRICE_KINDS) < set(_INGESTABLE_KINDS)


def test_clock_skew_last_run_after_now_stays_price_only() -> None:
    """ADR-0125 §4: the helper claims nothing beyond the date comparison.

    A ``last_run_at`` in the future on the same UTC day is not an earlier date,
    so the run is treated as intraday. The window arithmetic already clamps
    such skew (``_fetch_window``); the kind split needs no second rule.
    """
    last_run = _NOW + timedelta(hours=1)
    assert _kinds_for_run(_NOW, last_run) == _PRICE_KINDS
