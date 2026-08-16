# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Construction-validation and provider-blindness tests for the DTO (ADR-0091).

The DTO is the result contract: its four load-bearing properties are the
acceptance criteria for any adapter. These tests pin the construction-time
guarantees (property 3's edge-normalisation and the strict boundary) and the
provider-blindness golden test (property 1): the same logical series built via
the synthetic adapter and a mocked Yahoo adapter must be indistinguishable
except for the ``provider`` provenance field.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from services.market_data.adapters.synthetic import SyntheticAdapter
from services.market_data.adapters.yahoo import YahooAdapter
from services.market_data.dto import (
    IDENTIFIER_SCHEMES,
    DateWindow,
    NormalizedIdentifier,
    NormalizedQuote,
    NormalizedSeries,
    SeriesKind,
    SeriesPoint,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "market_data" / "synthetic_sample.json"


class TestNormalizedIdentifier:
    def test_value_is_trimmed_and_uppercased(self) -> None:
        ident = NormalizedIdentifier("ticker", "  aapl ")
        assert ident.value == "AAPL"
        assert ident.scheme == "ticker"

    def test_unknown_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown identifier scheme"):
            NormalizedIdentifier("sedol", "123")

    def test_all_adr_schemes_accepted(self) -> None:
        for scheme in IDENTIFIER_SCHEMES:
            assert NormalizedIdentifier(scheme, "x").scheme == scheme

    def test_empty_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty after trimming"):
            NormalizedIdentifier("isin", "   ")


class TestDateWindow:
    def test_valid_window(self) -> None:
        window = DateWindow(date(2026, 1, 1), date(2026, 1, 31))
        assert window.contains(date(2026, 1, 15))
        assert window.contains(date(2026, 1, 1))
        assert window.contains(date(2026, 1, 31))
        assert not window.contains(date(2025, 12, 31))
        assert not window.contains(date(2026, 2, 1))

    def test_start_after_end_rejected(self) -> None:
        with pytest.raises(ValueError, match="is after end"):
            DateWindow(date(2026, 2, 1), date(2026, 1, 1))

    def test_datetime_bound_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a datetime.date"):
            DateWindow(datetime(2026, 1, 1, 12, 0), date(2026, 1, 31))


class TestSeriesKind:
    def test_values_align_with_flow_type_and_nav(self) -> None:
        # Value-identity with the canonical flow_type / nav_kind literals is
        # what lets the ingest write path consume a kind without translation.
        assert SeriesKind.DIVIDEND == "dividend"
        assert SeriesKind.CAPITAL_CALL == "capital_call"
        assert SeriesKind.NAV_PRICE == "nav_price"

    def test_seven_cashflow_kinds_present(self) -> None:
        cashflow = {
            "capital_call",
            "distribution",
            "fee",
            "carry",
            "dividend",
            "coupon",
            "other",
        }
        assert cashflow <= {k.value for k in SeriesKind}

    def test_five_weight_families_present(self) -> None:
        weights = {
            "weight_sector",
            "weight_region",
            "weight_country",
            "weight_rating",
            "weight_maturity",
        }
        assert weights <= {k.value for k in SeriesKind}

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            SeriesKind("ytm")


class TestSeriesPoint:
    def test_float_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a decimal.Decimal"):
            SeriesPoint(date(2026, 1, 2), 185.5)  # type: ignore[arg-type]

    def test_datetime_date_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a datetime.date"):
            SeriesPoint(datetime(2026, 1, 2, 12), Decimal("1"))  # type: ignore[arg-type]

    def test_valid_point(self) -> None:
        point = SeriesPoint(date(2026, 1, 2), Decimal("185.5"))
        assert point.value == Decimal("185.5")


class TestNormalizedSeries:
    def _ident(self) -> NormalizedIdentifier:
        return NormalizedIdentifier("ticker", "AAPL")

    def test_ascending_points_accepted(self) -> None:
        series = NormalizedSeries(
            ident=self._ident(),
            provider="synthetic",
            kind=SeriesKind.NAV_PRICE,
            currency="usd",
            points=(
                SeriesPoint(date(2026, 1, 2), Decimal("1")),
                SeriesPoint(date(2026, 1, 3), Decimal("2")),
            ),
        )
        # currency is normalised (upper-cased) at construction.
        assert series.currency == "USD"
        assert isinstance(series.kind, SeriesKind)

    def test_kind_coerced_from_string(self) -> None:
        series = NormalizedSeries(
            ident=self._ident(),
            provider="synthetic",
            kind="dividend",  # type: ignore[arg-type]
            currency="EUR",
            points=(),
        )
        assert series.kind is SeriesKind.DIVIDEND

    def test_unordered_points_rejected(self) -> None:
        with pytest.raises(ValueError, match="strictly ascending"):
            NormalizedSeries(
                ident=self._ident(),
                provider="synthetic",
                kind=SeriesKind.NAV_PRICE,
                currency="EUR",
                points=(
                    SeriesPoint(date(2026, 1, 3), Decimal("2")),
                    SeriesPoint(date(2026, 1, 2), Decimal("1")),
                ),
            )

    def test_duplicate_dates_rejected(self) -> None:
        with pytest.raises(ValueError, match="strictly ascending"):
            NormalizedSeries(
                ident=self._ident(),
                provider="synthetic",
                kind=SeriesKind.NAV_PRICE,
                currency="EUR",
                points=(
                    SeriesPoint(date(2026, 1, 2), Decimal("1")),
                    SeriesPoint(date(2026, 1, 2), Decimal("2")),
                ),
            )

    def test_empty_currency_rejected(self) -> None:
        with pytest.raises(ValueError, match="currency must be non-empty"):
            NormalizedSeries(
                ident=self._ident(),
                provider="synthetic",
                kind=SeriesKind.NAV_PRICE,
                currency="  ",
                points=(),
            )

    def test_empty_provider_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider must be non-empty"):
            NormalizedSeries(
                ident=self._ident(),
                provider="",
                kind=SeriesKind.NAV_PRICE,
                currency="EUR",
                points=(),
            )

    def test_empty_points_allowed(self) -> None:
        # An empty series is a real "no data in window" gap — distinct from an
        # unsupported kind (which the capability matrix declares).
        series = NormalizedSeries(
            ident=self._ident(),
            provider="synthetic",
            kind=SeriesKind.DIVIDEND,
            currency="EUR",
            points=(),
        )
        assert series.points == ()

    def test_provider_field_is_only_provenance(self) -> None:
        # Provider-blindness (property 1): the DTO's field names carry no
        # provider-specific token; `provider` is a plain string.
        field_names = {f.name for f in dataclasses.fields(NormalizedSeries)}
        assert field_names == {"ident", "provider", "kind", "currency", "points"}


class TestNormalizedQuote:
    def test_to_series_round_trips(self) -> None:
        quote = NormalizedQuote(
            ident=NormalizedIdentifier("ticker", "AAPL"),
            provider="synthetic",
            kind=SeriesKind.NAV_PRICE,
            currency="usd",
            as_of_date=date(2026, 1, 2),
            value=Decimal("185.5"),
        )
        series = quote.to_series()
        assert series.currency == "USD"
        assert series.points == (SeriesPoint(date(2026, 1, 2), Decimal("185.5")),)

    def test_float_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a decimal.Decimal"):
            NormalizedQuote(
                ident=NormalizedIdentifier("ticker", "AAPL"),
                provider="synthetic",
                kind=SeriesKind.NAV_PRICE,
                currency="USD",
                as_of_date=date(2026, 1, 2),
                value=185.5,  # type: ignore[arg-type]
            )


def _yahoo_chart_payload() -> dict:
    """A recorded-shape Yahoo chart payload for AAPL, gmtoffset 0, USD."""
    ts1 = int(datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc).timestamp())
    ts2 = int(datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc).timestamp())
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": "AAPL",
                        "gmtoffset": 0,
                    },
                    "timestamp": [ts1, ts2],
                    "indicators": {"quote": [{"close": [185.5, 187.25]}]},
                }
            ],
            "error": None,
        }
    }


async def test_provider_blindness_golden(httpx_mock) -> None:
    """The same logical series is identical across providers except provenance.

    Build the AAPL nav_price series over the same window from (a) the synthetic
    adapter and (b) a mocked Yahoo adapter. The two DTOs must be equal in every
    field except ``provider`` — the golden test of provider-blindness.
    """
    httpx_mock.add_response(json=_yahoo_chart_payload())

    ident = NormalizedIdentifier("ticker", "AAPL")
    window = DateWindow(date(2026, 1, 1), date(2026, 1, 5))

    yahoo_series = await YahooAdapter().fetch_series(ident, SeriesKind.NAV_PRICE, window)
    synthetic_series = await SyntheticAdapter(_SAMPLE_FIXTURE, currency="USD").fetch_series(
        ident, SeriesKind.NAV_PRICE, window
    )

    assert yahoo_series.provider == "yahoo"
    assert synthetic_series.provider == "synthetic"

    # Neutralise only the provenance field; everything else must match.
    neutral_yahoo = dataclasses.replace(yahoo_series, provider="_")
    neutral_synthetic = dataclasses.replace(synthetic_series, provider="_")
    assert neutral_yahoo == neutral_synthetic

    # And the shared content is what we expect.
    assert neutral_yahoo.currency == "USD"
    assert neutral_yahoo.points == (
        SeriesPoint(date(2026, 1, 2), Decimal("185.5")),
        SeriesPoint(date(2026, 1, 5), Decimal("187.25")),
    )
