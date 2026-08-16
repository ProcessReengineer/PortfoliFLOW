# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Synthetic adapter tests: round-trip, determinism, window filtering (ADR-0091).

No network — the synthetic provider is fixture-driven. These pin the
test-event seam's guarantees: a fixture round-trips to canonical points, two
fetches are byte-identical (determinism), the window filters points, an unknown
identifier is unresolvable, and a kind absent for a known identifier is an
empty series (a real gap, not an error).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from services.market_data.adapters.synthetic import SyntheticAdapter
from services.market_data.dto import (
    DateWindow,
    NormalizedIdentifier,
    SeriesKind,
    SeriesPoint,
)
from services.market_data.provider import (
    IdentifierNotResolvableError,
    MarketDataConfigurationError,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "tests" / "fixtures" / "market_data" / "synthetic_sample.json"
_AAPL = NormalizedIdentifier("ticker", "AAPL")
_WIDE = DateWindow(date(2026, 1, 1), date(2026, 12, 31))


def _adapter(currency: str = "USD") -> SyntheticAdapter:
    return SyntheticAdapter(_SAMPLE, currency=currency)


class TestRoundTrip:
    async def test_nav_price_round_trips(self) -> None:
        series = await _adapter().fetch_series(_AAPL, SeriesKind.NAV_PRICE, _WIDE)
        assert series.provider == "synthetic"
        assert series.currency == "USD"
        assert series.points == (
            SeriesPoint(date(2026, 1, 2), Decimal("185.5")),
            SeriesPoint(date(2026, 1, 5), Decimal("187.25")),
            SeriesPoint(date(2026, 1, 6), Decimal("188.0")),
        )

    async def test_case_insensitive_identifier_lookup(self) -> None:
        # NormalizedIdentifier upper-cases the value; the fixture matches.
        lower = NormalizedIdentifier("ticker", "aapl")
        series = await _adapter().fetch_series(lower, SeriesKind.NAV_PRICE, _WIDE)
        assert len(series.points) == 3


class TestDeterminism:
    async def test_two_fetches_are_identical(self) -> None:
        adapter = _adapter()
        first = await adapter.fetch_series(_AAPL, SeriesKind.NAV_PRICE, _WIDE)
        second = await adapter.fetch_series(_AAPL, SeriesKind.NAV_PRICE, _WIDE)
        assert first == second


class TestWindowFiltering:
    async def test_points_outside_window_dropped(self) -> None:
        narrow = DateWindow(date(2026, 1, 1), date(2026, 1, 5))
        series = await _adapter().fetch_series(_AAPL, SeriesKind.NAV_PRICE, narrow)
        assert [p.as_of_date for p in series.points] == [
            date(2026, 1, 2),
            date(2026, 1, 5),
        ]


class TestGapsAndErrors:
    async def test_unknown_identifier_not_resolvable(self) -> None:
        with pytest.raises(IdentifierNotResolvableError):
            await _adapter().fetch_series(
                NormalizedIdentifier("ticker", "TSLA"),
                SeriesKind.NAV_PRICE,
                _WIDE,
            )

    async def test_known_id_missing_kind_is_empty_series(self) -> None:
        # MSFT has nav_price only; a dividend fetch is a real "no data" gap.
        msft = NormalizedIdentifier("ticker", "MSFT")
        series = await _adapter().fetch_series(msft, SeriesKind.DIVIDEND, _WIDE)
        assert series.points == ()
        assert series.kind is SeriesKind.DIVIDEND

    async def test_missing_fixture_file_raises(self, tmp_path) -> None:
        with pytest.raises(MarketDataConfigurationError):
            SyntheticAdapter(tmp_path / "does_not_exist.json")

    async def test_unknown_kind_key_in_fixture_raises(self, tmp_path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text('{"AAPL": {"ytm": []}}', encoding="utf-8")
        with pytest.raises(MarketDataConfigurationError):
            SyntheticAdapter(bad)

    async def test_doc_key_is_skipped(self, tmp_path) -> None:
        # A "__doc__" documentation key must not be treated as an identifier.
        f = tmp_path / "doc.json"
        f.write_text(
            '{"__doc__": "notes", "AAPL": {"nav_price": [["2026-01-02", "1.0"]]}}',
            encoding="utf-8",
        )
        series = await SyntheticAdapter(f).fetch_series(_AAPL, SeriesKind.NAV_PRICE, _WIDE)
        assert len(series.points) == 1


async def test_operator_sample_fixture_loads() -> None:
    # The shipped operator sample must itself be a loadable fixture.
    sample = _REPO_ROOT / "config" / "market_data_synthetic_example.json"
    adapter = SyntheticAdapter(sample)
    series = await adapter.fetch_series(
        NormalizedIdentifier("isin", "US0378331005"),
        SeriesKind.NAV_PRICE,
        _WIDE,
    )
    assert len(series.points) == 3
