# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Yahoo adapter tests against a mocked transport (ADR-0091).

No live network: every request is faked with ``httpx_mock`` (the codebase's
established HTTP-faking idiom, cf. ``tests/services/web_research/test_fetcher``).
The tests prove the in-adapter normalisation (property 3): unadjusted close as
``nav_price``, dividend events as positive cashflows, exchange-local statement
dates via ``gmtoffset``, null-close gap dropping, window filtering — plus the
error mapping onto the port error types.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from services.market_data.adapters.yahoo import YahooAdapter
from services.market_data.dto import (
    DateWindow,
    NormalizedIdentifier,
    SeriesKind,
)
from services.market_data.provider import (
    IdentifierNotResolvableError,
    ProviderFetchError,
    UnsupportedCapabilityError,
)

_TICKER = NormalizedIdentifier("ticker", "AAPL")
_WINDOW = DateWindow(date(2026, 1, 1), date(2026, 1, 31))


def _epoch(y: int, m: int, d: int, hour: int = 14) -> int:
    return int(datetime(y, m, d, hour, 30, tzinfo=timezone.utc).timestamp())


def _price_payload(*, gmtoffset: int = 0) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": "AAPL",
                        "gmtoffset": gmtoffset,
                    },
                    "timestamp": [
                        _epoch(2026, 1, 2),
                        _epoch(2026, 1, 5),
                        _epoch(2026, 1, 6),
                    ],
                    # A null close (market holiday) must be dropped as a gap.
                    "indicators": {"quote": [{"close": [185.5, None, 187.25]}]},
                }
            ],
            "error": None,
        }
    }


def _dividend_payload() -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": "AAPL",
                        "gmtoffset": 0,
                    },
                    "timestamp": [_epoch(2026, 1, 2)],
                    "indicators": {"quote": [{"close": [185.5]}]},
                    "events": {
                        "dividends": {
                            str(_epoch(2026, 1, 9)): {
                                "amount": 0.24,
                                "date": _epoch(2026, 1, 9),
                            }
                        }
                    },
                }
            ],
            "error": None,
        }
    }


class TestNavPrice:
    async def test_happy_path_maps_close_and_currency(self, httpx_mock) -> None:
        httpx_mock.add_response(json=_price_payload())
        series = await YahooAdapter().fetch_series(_TICKER, SeriesKind.NAV_PRICE, _WINDOW)
        assert series.provider == "yahoo"
        assert series.currency == "USD"
        assert series.kind is SeriesKind.NAV_PRICE
        # The null-close point (2026-01-05) is dropped; two real points remain.
        assert series.points[0].as_of_date == date(2026, 1, 2)
        assert series.points[0].value == Decimal("185.5")
        assert series.points[-1].as_of_date == date(2026, 1, 6)
        assert series.points[-1].value == Decimal("187.25")
        assert len(series.points) == 2

    async def test_gmtoffset_shifts_statement_date(self, httpx_mock) -> None:
        # A large negative gmtoffset pulls a just-after-midnight-UTC timestamp
        # back to the previous exchange-local calendar day.
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": "USD",
                            "gmtoffset": -18000,  # US Eastern, -5h
                        },
                        # 2026-01-06 02:00 UTC → 2026-01-05 21:00 local.
                        "timestamp": [
                            int(datetime(2026, 1, 6, 2, 0, tzinfo=timezone.utc).timestamp())
                        ],
                        "indicators": {"quote": [{"close": [100.0]}]},
                    }
                ],
                "error": None,
            }
        }
        httpx_mock.add_response(json=payload)
        series = await YahooAdapter().fetch_series(_TICKER, SeriesKind.NAV_PRICE, _WINDOW)
        assert series.points[0].as_of_date == date(2026, 1, 5)

    async def test_points_outside_window_filtered(self, httpx_mock) -> None:
        httpx_mock.add_response(json=_price_payload())
        narrow = DateWindow(date(2026, 1, 1), date(2026, 1, 2))
        series = await YahooAdapter().fetch_series(_TICKER, SeriesKind.NAV_PRICE, narrow)
        assert [p.as_of_date for p in series.points] == [date(2026, 1, 2)]


class TestDividend:
    async def test_happy_path_maps_dividend_events(self, httpx_mock) -> None:
        httpx_mock.add_response(json=_dividend_payload())
        series = await YahooAdapter().fetch_series(_TICKER, SeriesKind.DIVIDEND, _WINDOW)
        assert series.kind is SeriesKind.DIVIDEND
        assert len(series.points) == 1
        assert series.points[0].as_of_date == date(2026, 1, 9)
        # Dividends are positive (a distribution to the holder).
        assert series.points[0].value == Decimal("0.24")


class TestErrorMapping:
    async def test_unsupported_scheme_raises_before_call(self) -> None:
        with pytest.raises(UnsupportedCapabilityError):
            await YahooAdapter().fetch_series(
                NormalizedIdentifier("isin", "US0378331005"),
                SeriesKind.NAV_PRICE,
                _WINDOW,
            )

    async def test_unsupported_kind_raises_before_call(self) -> None:
        with pytest.raises(UnsupportedCapabilityError):
            await YahooAdapter().fetch_series(_TICKER, SeriesKind.COUPON, _WINDOW)

    async def test_yahoo_error_payload_maps_to_not_resolvable(self, httpx_mock) -> None:
        httpx_mock.add_response(
            status_code=404,
            json={
                "chart": {
                    "result": None,
                    "error": {
                        "code": "Not Found",
                        "description": "No data found, symbol may be delisted",
                    },
                }
            },
        )
        with pytest.raises(IdentifierNotResolvableError):
            await YahooAdapter().fetch_series(_TICKER, SeriesKind.NAV_PRICE, _WINDOW)

    async def test_http_500_maps_to_fetch_error(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=500, text="upstream boom")
        with pytest.raises(ProviderFetchError, match="HTTP 500"):
            await YahooAdapter().fetch_series(_TICKER, SeriesKind.NAV_PRICE, _WINDOW)

    async def test_timeout_maps_to_fetch_error(self, httpx_mock) -> None:
        httpx_mock.add_exception(httpx.ReadTimeout("read timed out"))
        with pytest.raises(ProviderFetchError, match="[Tt]imeout"):
            await YahooAdapter(timeout=0.01).fetch_series(_TICKER, SeriesKind.NAV_PRICE, _WINDOW)

    async def test_missing_currency_maps_to_fetch_error(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {"symbol": "AAPL"},
                            "timestamp": [],
                            "indicators": {"quote": [{"close": []}]},
                        }
                    ],
                    "error": None,
                }
            }
        )
        with pytest.raises(ProviderFetchError, match="no currency"):
            await YahooAdapter().fetch_series(_TICKER, SeriesKind.NAV_PRICE, _WINDOW)
