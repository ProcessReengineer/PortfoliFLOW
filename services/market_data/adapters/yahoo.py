# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Yahoo Finance adapter — native async EOD prices and dividends (ADR-0091).

Implements :class:`services.market_data.provider.MarketDataProvider` over
Yahoo's free, keyless public v8 chart endpoint using ``httpx.AsyncClient``.
No bridging: this provider is natively async.

Coverage (encoded in ``config/market_data_capabilities.yaml``):

- **scheme:** ``ticker`` only. The chart endpoint is addressed by exchange
  ticker symbol; it does not accept an ISIN. ISIN→ticker/FIGI resolution is
  OpenFIGI's separate concern (``services/market_data/normalisation.py``).
- **kinds:** ``nav_price`` and ``dividend``. Bond metrics (coupon, YTM,
  duration) and composition weights are not exposed by this endpoint and are
  deliberately absent from the matrix — an empirical narrowing recorded for
  the slice-3 handover.

**Normalisation decisions made inside this adapter** (ADR-0091 property 3 —
paid once here so everything downstream stays provider-blind):

- *Price basis:* ``nav_price`` uses the **unadjusted** close, not the adjusted
  close. Adjusted close retroactively rewrites history for splits and
  dividends; a statement-day NAV series must reflect the actual value on each
  day. Dividends are captured separately as the ``dividend`` cashflow kind, so
  using unadjusted close here avoids double-counting the dividend effect.
- *Statement day:* Yahoo timestamps are epoch seconds at the session start in
  the exchange timezone. The calendar date is derived by applying the
  response's ``meta.gmtoffset`` before taking the date, yielding the
  exchange-local trading day — the honest statement day the ``investment_navs``
  DATE column holds. A ``dividend`` point carries the same date-level
  convention; the 12:00-UTC statement-day timestamp the extractor uses
  (ADR-0043 §3) is applied downstream by the write path at the date the DTO
  carries.
- *Scale & currency:* Yahoo returns unit-scale values (not thousands) in the
  instrument's own currency (``meta.currency``); no scaling is applied and the
  currency is carried through verbatim.
- *Gaps:* a ``null`` close (market holiday interleaved in the series) is a real
  gap and is dropped, not coerced to zero.

No retries are performed in this slice — retry policy belongs to the tick job
(ADR-0093). Timeouts and HTTP/transport failures map onto the port error
types; a Yahoo "symbol not found" error payload maps to
:class:`IdentifierNotResolvableError`.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from services.market_data.dto import (
    DateWindow,
    NormalizedIdentifier,
    NormalizedSeries,
    SeriesKind,
    SeriesPoint,
)
from services.market_data.provider import (
    IdentifierNotResolvableError,
    ProviderFetchError,
    UnsupportedCapabilityError,
)

_PROVIDER_NAME = "yahoo"

# Yahoo's public chart endpoint. The trailing symbol is appended per request.
_CHART_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

_SUPPORTED_SCHEME = "ticker"
_SUPPORTED_KINDS: frozenset[SeriesKind] = frozenset({SeriesKind.NAV_PRICE, SeriesKind.DIVIDEND})

# Identifies PortfoliFLOW so operators can recognise and rate-limit the
# traffic (same convention as services/web_research/fetcher.py).
_USER_AGENT = (
    "PortfoliFLOW-MarketData/0.1 "
    "(+https://github.com/ProcessReengineer/PortfoliFLOW; "
    "automated market-data fetch for institutional portfolio management)"
)

_DEFAULT_TIMEOUT = 8.0


class YahooAdapter:
    """Fetch EOD prices and dividends from Yahoo Finance for a ticker."""

    def __init__(self, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        """Initialise the adapter.

        Args:
            timeout: Per-request timeout in seconds. No retries are attempted
                (retry policy is the tick job's concern, ADR-0093).
        """
        self._timeout = timeout

    async def fetch_series(
        self,
        ident: NormalizedIdentifier,
        kind: SeriesKind,
        window: DateWindow,
    ) -> NormalizedSeries:
        """Fetch the ``kind`` series for ``ident`` over ``window``.

        See :meth:`MarketDataProvider.fetch_series`. Yahoo serves only
        ``ticker``-scheme identifiers and the ``nav_price`` / ``dividend``
        kinds; anything else raises :class:`UnsupportedCapabilityError` before
        any network call.

        Raises:
            UnsupportedCapabilityError: If ``ident.scheme`` is not ``ticker``
                or ``kind`` is unsupported.
            IdentifierNotResolvableError: If Yahoo reports the symbol unknown.
            ProviderFetchError: On timeout, transport failure, non-2xx status,
                or a malformed response.
        """
        if ident.scheme != _SUPPORTED_SCHEME:
            raise UnsupportedCapabilityError(
                f"Yahoo serves only scheme {_SUPPORTED_SCHEME!r}, not {ident.scheme!r}."
            )
        if kind not in _SUPPORTED_KINDS:
            raise UnsupportedCapabilityError(f"Yahoo does not serve kind {kind.value!r}.")

        symbol = ident.value
        result, currency, gmtoffset = await self._fetch_chart(symbol, kind, window)

        if kind is SeriesKind.NAV_PRICE:
            points_by_date = self._extract_prices(result, gmtoffset, window)
        else:  # SeriesKind.DIVIDEND
            points_by_date = self._extract_dividends(result, gmtoffset, window)

        points = tuple(
            SeriesPoint(as_of_date=day, value=value)
            for day, value in sorted(points_by_date.items())
        )
        return NormalizedSeries(
            ident=ident,
            provider=_PROVIDER_NAME,
            kind=kind,
            currency=currency,
            points=points,
        )

    async def _fetch_chart(
        self, symbol: str, kind: SeriesKind, window: DateWindow
    ) -> tuple[dict[str, Any], str, int]:
        """GET the chart payload and return ``(result, currency, gmtoffset)``.

        Raises:
            IdentifierNotResolvableError: On a Yahoo error payload / empty
                result.
            ProviderFetchError: On timeout, transport error, non-2xx status, or
                a response missing the currency.
        """
        params: dict[str, Any] = {
            "period1": _to_epoch(window.start),
            # period2 is exclusive of its instant; add a day to include `end`.
            "period2": _to_epoch(window.end + timedelta(days=1)),
            "interval": "1d",
        }
        if kind is SeriesKind.DIVIDEND:
            params["events"] = "div"

        url = f"{_CHART_BASE_URL}{symbol}"
        headers = {"User-Agent": _USER_AGENT}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=self._timeout) as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderFetchError(f"Timeout fetching Yahoo chart for {symbol!r}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderFetchError(
                f"HTTP error fetching Yahoo chart for {symbol!r}: {exc}"
            ) from exc

        payload = _safe_json(response)
        chart = payload.get("chart") if isinstance(payload, dict) else None
        error = chart.get("error") if isinstance(chart, dict) else None
        if error:
            raise IdentifierNotResolvableError(
                f"Yahoo could not resolve symbol {symbol!r}: {error}."
            )
        if response.status_code >= 400:
            raise ProviderFetchError(f"Yahoo returned HTTP {response.status_code} for {symbol!r}.")

        result_list = chart.get("result") if isinstance(chart, dict) else None
        if not result_list:
            raise IdentifierNotResolvableError(f"Yahoo returned no result for symbol {symbol!r}.")
        result = result_list[0]
        meta = result.get("meta") if isinstance(result, dict) else None
        currency = meta.get("currency") if isinstance(meta, dict) else None
        if not currency:
            raise ProviderFetchError(f"Yahoo response for {symbol!r} carries no currency.")
        gmtoffset = (meta.get("gmtoffset") if isinstance(meta, dict) else None) or 0
        return result, currency, int(gmtoffset)

    @staticmethod
    def _extract_prices(
        result: dict[str, Any], gmtoffset: int, window: DateWindow
    ) -> dict[Any, Decimal]:
        """Map the unadjusted close array to ``{date: Decimal}`` within window.

        Null closes (interleaved market holidays) are dropped as real gaps.
        """
        timestamps = result.get("timestamp") or []
        quote_blocks = (result.get("indicators") or {}).get("quote") or [{}]
        closes = (quote_blocks[0] or {}).get("close") or []
        points_by_date: dict[Any, Decimal] = {}
        for unix_ts, close in zip(timestamps, closes):
            if unix_ts is None or close is None:
                continue
            day = _local_date(unix_ts, gmtoffset)
            if not window.contains(day):
                continue
            points_by_date[day] = Decimal(str(close))
        return points_by_date

    @staticmethod
    def _extract_dividends(
        result: dict[str, Any], gmtoffset: int, window: DateWindow
    ) -> dict[Any, Decimal]:
        """Map the dividend events to ``{date: Decimal}`` within window.

        Dividend amounts are positive (a distribution to the holder), matching
        the sign convention of the ``distribution`` / ``dividend`` cashflow
        kinds.
        """
        dividends = (result.get("events") or {}).get("dividends") or {}
        points_by_date: dict[Any, Decimal] = {}
        for entry in dividends.values():
            if not isinstance(entry, dict):
                continue
            amount = entry.get("amount")
            unix_ts = entry.get("date")
            if amount is None or unix_ts is None:
                continue
            day = _local_date(unix_ts, gmtoffset)
            if not window.contains(day):
                continue
            points_by_date[day] = Decimal(str(amount))
        return points_by_date


def _to_epoch(day: Any) -> int:
    """Return the UTC-midnight epoch seconds for a calendar ``day``."""
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())


def _local_date(unix_ts: Any, gmtoffset: int) -> Any:
    """Return the exchange-local calendar date for ``unix_ts``.

    Applies ``gmtoffset`` (seconds) before taking the date, so a session-start
    timestamp resolves to its exchange-local trading day rather than a UTC date
    that could roll across midnight for far-from-UTC exchanges.
    """
    return datetime.fromtimestamp(int(unix_ts) + gmtoffset, tz=timezone.utc).date()


def _safe_json(response: httpx.Response) -> Any:
    """Return the parsed JSON body, or ``{}`` if the body is not JSON."""
    try:
        return response.json()
    except ValueError:
        return {}
