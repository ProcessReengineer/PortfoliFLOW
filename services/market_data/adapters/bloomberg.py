# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Bloomberg Desktop-API adapter — synchronous ``blpapi`` bridged locally (ADR-0091).

Implements :class:`services.market_data.provider.MarketDataProvider` against a
locally running, entitled Bloomberg Terminal via the Desktop API
(``//blp/refdata``). Bloomberg's SDK (``blpapi``) is **synchronous**; async is
the rule everywhere else on this platform, so — per ADR-0091 §"Adapters bridge
locally, not the port" — the bridge lives *inside this one adapter*: every
gateway call funnels through a single :func:`asyncio.to_thread` site
(:meth:`BloombergAdapter._run`), the port never changes shape, and nothing else
in the adapter blocks the event loop.

**The gateway seam.** The adapter core never touches a ``blpapi`` object. It
converses with a small **synchronous** :class:`BloombergGateway` protocol —
plain-dict requests in, plain-dict (already event-flattened) responses out.
Only :class:`BlpapiDesktopGateway`, the real implementation, imports ``blpapi``
— **lazily, inside its own method**, raising
:class:`~services.market_data.provider.MarketDataConfigurationError` with an
actionable install hint when the import fails. Tests substitute a fake gateway;
no ``blpapi`` object is ever mocked, and the whole suite passes on a machine
without the package (it is NOT on public PyPI — Bloomberg hosts its own index).

**Coverage this slice actually implements** (encoded in
``config/market_data_capabilities.yaml``, ``enabled: false`` until a Terminal
machine is available):

- **schemes:** ``figi`` → security topic ``/bbgid/<FIGI>`` (a FIGI *is*
  Bloomberg's BBGID) and ``isin`` → ``/isin/<ISIN>``. ``ticker`` is deliberately
  unsupported: a plain Bloomberg ticker needs the yellow-key suffix
  ("… US Equity"), which the identifier model does not store — guessing it
  would violate the key-forming discipline. Bloomberg's coverage is therefore
  *disjoint* from Yahoo's (ticker); it extends routing to ISIN/FIGI-primary
  investments rather than competing.
- **kinds:** ``nav_price`` only. ``dividend`` is **deferred** in this slice —
  the ``DVD_HIST_ALL`` dividend-amount currency (currency-of-declaration vs.
  ``CRNCY``) is a material ambiguity that cannot be resolved from the official
  documentation without a live Terminal to confirm against (#036 §1.5). Per the
  honesty gate, the adapter ships ``nav_price``-only rather than guess; the open
  question is recorded in the slice hand-over.

**Normalisation paid inside the adapter** (ADR-0091 property 3):

- *Price basis:* ``nav_price`` is the daily ``PX_LAST`` from a
  ``HistoricalDataRequest`` (``periodicitySelection=DAILY``). Bloomberg's
  historical date range is inclusive of both ``startDate`` and ``endDate``; the
  request dates use Bloomberg's ``YYYYMMDD`` form. A ``fieldData`` row missing
  ``PX_LAST`` (a holiday / halted day may return a date-only row) is skipped as
  a real gap, never coerced.
- *Currency:* the DTO requires a non-empty currency and it must come from
  Bloomberg metadata, never guessed or defaulted. The adapter issues one extra
  ``ReferenceDataRequest`` for the static ``CRNCY`` field on the same security;
  a missing/empty ``CRNCY`` is a :class:`ProviderFetchError`, not a silent
  fallback.

**Error mapping onto the port** (ADR-0091; no ``blpapi`` exception ever crosses
this boundary): ``securityError`` → :class:`IdentifierNotResolvableError`;
``responseError`` / ``fieldExceptions`` / any session-service failure the
gateway surfaces (as :class:`BloombergGatewayError`) →
:class:`ProviderFetchError`; an unsupported scheme/kind →
:class:`UnsupportedCapabilityError`. No retries are performed — retry policy and
error containment belong to the tick job (ADR-0093).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from services.market_data.dto import (
    DateWindow,
    NormalizedIdentifier,
    NormalizedSeries,
    SeriesKind,
    SeriesPoint,
)
from services.market_data.provider import (
    IdentifierNotResolvableError,
    MarketDataConfigurationError,
    ProviderFetchError,
    UnsupportedCapabilityError,
)

_PROVIDER_NAME = "bloomberg"

# Reference-data service and the two request operations the adapter uses.
_REFDATA_SERVICE = "//blp/refdata"
_HISTORICAL_OP = "HistoricalDataRequest"
_REFERENCE_OP = "ReferenceDataRequest"

# The fields fetched: daily last price for the series, static currency metadata.
_PX_LAST = "PX_LAST"
_CRNCY = "CRNCY"

# Scheme → Bloomberg security-topic prefix. Values arrive repository-normalised
# (trimmed + upper-cased); they are used verbatim. Any scheme absent here is
# unsupported (see the module docstring for why ``ticker`` is excluded).
_SCHEME_TOPIC_PREFIX: dict[str, str] = {
    "figi": "/bbgid/",
    "isin": "/isin/",
}

_SUPPORTED_KINDS: frozenset[SeriesKind] = frozenset({SeriesKind.NAV_PRICE})

# Bloomberg's own pip index — ``blpapi`` is NOT on public PyPI. Surfaced in the
# configuration error so an operator on a Terminal machine can install it.
_BLPAPI_PIP_INDEX = "https://blpapi.bloomberg.com/repository/releases/python/simple/"


class BloombergGatewayError(Exception):
    """A session- or service-level failure surfaced by a gateway.

    Deliberately **not** a :class:`~services.market_data.provider.MarketDataError`:
    it is the gateway's private failure channel. The adapter catches it at the
    single bridge site and re-raises it as :class:`ProviderFetchError`, so no
    gateway internal — and, for the real gateway, no ``blpapi`` exception type —
    ever crosses the port (ADR-0091 property 3).
    """


@runtime_checkable
class BloombergGateway(Protocol):
    """The synchronous request/response seam the adapter converses over.

    Plain-dict requests in, plain-dict (already event-flattened) responses out,
    so the adapter core is ``blpapi``-blind and a test can substitute a fake
    without mocking the SDK. A request is a mapping with keys ``service``,
    ``operation``, ``security``, ``fields``, and ``options``. A response is a
    mapping with keys ``securityError`` / ``responseError`` (falsy when absent),
    ``fieldExceptions`` (a possibly-empty list), and ``fieldData`` (a list of
    per-date rows for a historical request, a field→value mapping for a
    reference request).
    """

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run one synchronous request and return the flattened response.

        Raises:
            BloombergGatewayError: On a session/service-level failure.
            MarketDataConfigurationError: If the provider SDK is unavailable
                (the real gateway's lazy-import failure).
        """
        ...


class BloombergAdapter:
    """Fetch a ``nav_price`` series for one security via a gateway seam."""

    def __init__(self, gateway: BloombergGateway) -> None:
        """Initialise the adapter over a gateway.

        Args:
            gateway: The synchronous :class:`BloombergGateway` the adapter
                drives. In production this is a :class:`BlpapiDesktopGateway`;
                tests pass a fake.
        """
        self._gateway = gateway

    async def fetch_series(
        self,
        ident: NormalizedIdentifier,
        kind: SeriesKind,
        window: DateWindow,
    ) -> NormalizedSeries:
        """Fetch the ``kind`` series for ``ident`` over ``window``.

        See :meth:`MarketDataProvider.fetch_series`. Bloomberg serves only the
        ``figi`` / ``isin`` schemes and the ``nav_price`` kind; anything else
        raises :class:`UnsupportedCapabilityError` before any gateway call.

        Raises:
            UnsupportedCapabilityError: If ``ident.scheme`` or ``kind`` is not
                served.
            IdentifierNotResolvableError: If Bloomberg cannot resolve the
                security (``securityError``).
            ProviderFetchError: On a ``responseError`` / field exception, an
                empty ``CRNCY``, or a gateway session/service failure.
        """
        prefix = _SCHEME_TOPIC_PREFIX.get(ident.scheme)
        if prefix is None:
            raise UnsupportedCapabilityError(
                f"Bloomberg serves only schemes "
                f"{sorted(_SCHEME_TOPIC_PREFIX)}, not {ident.scheme!r}."
            )
        if kind not in _SUPPORTED_KINDS:
            raise UnsupportedCapabilityError(
                f"Bloomberg adapter does not serve kind {kind.value!r}."
            )
        topic = f"{prefix}{ident.value}"

        # Price history first: a bad security surfaces its securityError here.
        historical = await self._run(self._historical_request(topic, window))
        self._raise_on_errors(historical, topic, field=_PX_LAST)
        points = self._read_prices(historical, window)

        # Currency comes from Bloomberg metadata, never a default — one extra
        # ReferenceDataRequest on the same security (ADR-0091 property 3).
        currency = await self._fetch_currency(topic)

        return NormalizedSeries(
            ident=ident,
            provider=_PROVIDER_NAME,
            kind=kind,
            currency=currency,
            points=points,
        )

    async def _run(self, request: dict[str, Any]) -> dict[str, Any]:
        """The single async↔sync bridge site (ADR-0091 §"Adapters bridge locally").

        Every gateway call funnels through here so the blocking ``blpapi``
        exchange runs in a worker thread and the event loop stays free. A
        session/service failure the gateway raises as
        :class:`BloombergGatewayError` is mapped onto
        :class:`ProviderFetchError` — the gateway's internal failure type never
        crosses the port. A :class:`MarketDataConfigurationError` (the real
        gateway's lazy-import failure) is *not* a gateway error and propagates
        unchanged.
        """
        try:
            return await asyncio.to_thread(self._gateway.execute, request)
        except BloombergGatewayError as exc:
            raise ProviderFetchError(f"Bloomberg gateway failure: {exc}") from exc

    async def _fetch_currency(self, topic: str) -> str:
        """Fetch the security's ``CRNCY`` reference field.

        Raises:
            IdentifierNotResolvableError / ProviderFetchError: Via
                :meth:`_raise_on_errors` on an error response.
            ProviderFetchError: If ``CRNCY`` is absent or empty — the DTO
                requires a currency and it must come from provider metadata.
        """
        response = await self._run(self._reference_request(topic, [_CRNCY]))
        self._raise_on_errors(response, topic, field=_CRNCY)
        field_data = response.get("fieldData")
        currency = field_data.get(_CRNCY) if isinstance(field_data, dict) else None
        if not currency or not str(currency).strip():
            raise ProviderFetchError(
                f"Bloomberg returned no {_CRNCY} for {topic!r}; the series "
                "currency must come from provider metadata, never a default "
                "(ADR-0091 property 3)."
            )
        return str(currency)

    @staticmethod
    def _historical_request(topic: str, window: DateWindow) -> dict[str, Any]:
        """Build the ``PX_LAST`` daily ``HistoricalDataRequest`` for ``window``.

        Bloomberg's historical range is inclusive of both bounds and its dates
        use the ``YYYYMMDD`` form.
        """
        return {
            "service": _REFDATA_SERVICE,
            "operation": _HISTORICAL_OP,
            "security": topic,
            "fields": [_PX_LAST],
            "options": {
                "periodicitySelection": "DAILY",
                "startDate": window.start.strftime("%Y%m%d"),
                "endDate": window.end.strftime("%Y%m%d"),
            },
        }

    @staticmethod
    def _reference_request(topic: str, fields: list[str]) -> dict[str, Any]:
        """Build a static ``ReferenceDataRequest`` for ``fields`` on ``topic``."""
        return {
            "service": _REFDATA_SERVICE,
            "operation": _REFERENCE_OP,
            "security": topic,
            "fields": list(fields),
            "options": {},
        }

    @staticmethod
    def _raise_on_errors(response: dict[str, Any], topic: str, *, field: str) -> None:
        """Map a response's error payloads onto the port error types.

        ``securityError`` is an unresolvable identifier; ``responseError`` and
        field exceptions are fetch failures (ADR-0091).

        Raises:
            IdentifierNotResolvableError: On a ``securityError``.
            ProviderFetchError: On a ``responseError`` or field exception(s).
        """
        if response.get("securityError"):
            raise IdentifierNotResolvableError(
                f"Bloomberg could not resolve {topic!r}: {response['securityError']}."
            )
        if response.get("responseError"):
            raise ProviderFetchError(
                f"Bloomberg responseError for {topic!r}: {response['responseError']}."
            )
        field_exceptions = response.get("fieldExceptions") or []
        if field_exceptions:
            raise ProviderFetchError(
                f"Bloomberg field exception(s) for {topic!r} on {field!r}: {field_exceptions}."
            )

    @staticmethod
    def _read_prices(response: dict[str, Any], window: DateWindow) -> tuple[SeriesPoint, ...]:
        """Map ``fieldData`` rows to ordered ``PX_LAST`` points within ``window``.

        A row missing ``PX_LAST`` (a date-only holiday/halted row) is skipped as
        a real gap. Points are keyed by date so duplicates collapse, then
        emitted strictly ascending for the DTO.
        """
        rows = response.get("fieldData") or []
        points_by_date: dict[date, Decimal] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_value = row.get(_PX_LAST)
            raw_date = row.get("date")
            if raw_value is None or raw_date is None:
                continue
            day = _parse_date(raw_date)
            if not window.contains(day):
                continue
            points_by_date[day] = _parse_decimal(raw_value)
        return tuple(
            SeriesPoint(as_of_date=day, value=value)
            for day, value in sorted(points_by_date.items())
        )


def _parse_date(raw: Any) -> date:
    """Return a plain :class:`date` for a flattened ``date`` field.

    Accepts an ISO ``YYYY-MM-DD`` string (the real gateway's flattened form) or
    a :class:`date` instance defensively.

    Raises:
        ProviderFetchError: If ``raw`` is not an ISO date.
    """
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ProviderFetchError(f"Bloomberg returned a non-ISO date {raw!r}.") from exc


def _parse_decimal(raw: Any) -> Decimal:
    """Return a :class:`Decimal` for a flattened ``PX_LAST`` value.

    ``str(raw)`` preserves the vendor's decimal text (avoiding a float detour)
    for the real gateway, and passes Decimals/strings through unchanged in tests.

    Raises:
        ProviderFetchError: If ``raw`` is not a valid decimal.
    """
    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise ProviderFetchError(
            f"Bloomberg returned a non-decimal {_PX_LAST} value {raw!r}."
        ) from exc


class BlpapiDesktopGateway:
    """The real Desktop-API gateway — lazily imports and drives ``blpapi``.

    Session lifecycle is **per-fetch** in v0: one ``Session`` is opened, used,
    and stopped per :meth:`execute` call. Simple and correct; session pooling or
    a dedicated single worker thread is a later optimisation, not built here.
    The connection settings (``host`` / ``port``) come from the factory, which
    reads them from the environment (ADR-0091 §"Factory & config layering"), so
    this gateway — and the adapter above it — stay credential-source-blind
    (ADR-0095 §1); the local Terminal session is the auth/entitlement boundary.

    NOTE: everything below the lazy ``blpapi`` import is the **live-only** path.
    It is exercised only against a running, entitled Terminal and is
    deliberately *not* covered by the fixture-validated tests (those drive a
    fake gateway); the live smoke against a real Terminal is the gated
    activation step (#036 deferred track).
    """

    def __init__(self, *, host: str, port: int) -> None:
        """Store the connection settings. Does **not** import ``blpapi``.

        Constructing the gateway (and therefore building the factory) must not
        raise on a machine without ``blpapi``; the import is deferred to
        :meth:`execute`.
        """
        self._host = host
        self._port = port

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run one refdata request against the local Terminal, synchronously.

        Opens a fresh session, sends the request, drains the response events
        into the flat seam shape, and always stops the session.

        Raises:
            MarketDataConfigurationError: If ``blpapi`` cannot be imported.
            BloombergGatewayError: On any session/service/SDK failure — no
                ``blpapi`` exception type is allowed to escape.
        """
        blpapi = self._import_blpapi()
        session = None
        try:
            options = blpapi.SessionOptions()
            options.setServerHost(self._host)
            options.setServerPort(self._port)
            session = blpapi.Session(options)
            if not session.start():
                raise BloombergGatewayError(
                    f"Could not start a Bloomberg session to "
                    f"{self._host}:{self._port} — is a Terminal running and "
                    "logged in on this host?"
                )
            service_name = request["service"]
            if not session.openService(service_name):
                raise BloombergGatewayError(f"Could not open Bloomberg service {service_name!r}.")
            service = session.getService(service_name)
            session.sendRequest(self._compose(service, request))
            return self._drain(session, blpapi, request["operation"])
        except BloombergGatewayError:
            raise
        except MarketDataConfigurationError:
            raise
        except Exception as exc:  # blpapi.Exception and kin — never let it cross.
            raise BloombergGatewayError(f"Bloomberg session/service failure: {exc}") from exc
        finally:
            if session is not None:
                # Best-effort teardown — never let a stop() failure cross.
                with contextlib.suppress(Exception):
                    session.stop()

    @staticmethod
    def _import_blpapi() -> Any:
        """Import ``blpapi`` lazily, translating its absence to a config error.

        Raises:
            MarketDataConfigurationError: If ``blpapi`` is not installed, with an
                actionable install hint (it is not on public PyPI).
        """
        try:
            # blpapi is not on public PyPI, so it is unresolvable in every
            # environment CI can build — the typechecker is told so here, and
            # the ImportError branch below is the supported path.
            # Lazy by design (#036 §0.4).
            import blpapi  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as exc:
            raise MarketDataConfigurationError(
                "The 'blpapi' package is required for the Bloomberg adapter but "
                "is not installed. It is NOT available on public PyPI — install "
                "it from Bloomberg's own index: "
                f"`pip install --index-url {_BLPAPI_PIP_INDEX} blpapi`. A "
                "locally running, entitled Bloomberg Terminal is also required. "
                "Keep the bloomberg capability-matrix entry disabled "
                "(enabled: false) until both exist."
            ) from exc
        return blpapi

    @staticmethod
    def _compose(service: Any, request: dict[str, Any]) -> Any:
        """Build a ``blpapi`` request from the plain-dict seam request."""
        blp_request = service.createRequest(request["operation"])
        securities = blp_request.getElement("securities")
        securities.appendValue(request["security"])
        fields = blp_request.getElement("fields")
        for field in request["fields"]:
            fields.appendValue(field)
        for key, value in (request.get("options") or {}).items():
            blp_request.set(key, value)
        return blp_request

    @classmethod
    def _drain(cls, session: Any, blpapi: Any, operation: str) -> dict[str, Any]:
        """Drain PARTIAL_RESPONSE / RESPONSE events into the flat seam shape."""
        flat: dict[str, Any] = {
            "securityError": None,
            "responseError": None,
            "fieldExceptions": [],
            "fieldData": [] if operation == _HISTORICAL_OP else {},
        }
        while True:
            event = session.nextEvent(500)
            event_type = event.eventType()
            if event_type in (
                blpapi.Event.PARTIAL_RESPONSE,
                blpapi.Event.RESPONSE,
            ):
                for message in event:
                    cls._flatten_message(message, operation, flat)
            if event_type == blpapi.Event.RESPONSE:
                break
        return flat

    @classmethod
    def _flatten_message(cls, message: Any, operation: str, flat: dict[str, Any]) -> None:
        """Flatten one ``blpapi`` message into ``flat`` in place."""
        if message.hasElement("responseError", True):
            flat["responseError"] = str(message.getElement("responseError"))
            return
        security_data = message.getElement("securityData")
        if operation == _HISTORICAL_OP:
            # HistoricalDataRequest: securityData is a single element carrying a
            # fieldData array of per-date rows.
            cls._flatten_security(security_data, flat, historical=True)
        else:
            # ReferenceDataRequest: securityData is an array, one entry per
            # requested security (we request exactly one).
            for i in range(security_data.numValues()):
                cls._flatten_security(security_data.getValueAsElement(i), flat, historical=False)

    @classmethod
    def _flatten_security(
        cls, security_data: Any, flat: dict[str, Any], *, historical: bool
    ) -> None:
        """Flatten one securityData element (errors, field exceptions, data)."""
        if security_data.hasElement("securityError", True):
            flat["securityError"] = str(security_data.getElement("securityError"))
            return
        if security_data.hasElement("fieldExceptions", True):
            exceptions = security_data.getElement("fieldExceptions")
            for i in range(exceptions.numValues()):
                flat["fieldExceptions"].append(str(exceptions.getValueAsElement(i)))
        field_data = security_data.getElement("fieldData")
        if historical:
            for i in range(field_data.numValues()):
                flat["fieldData"].append(cls._flatten_row(field_data.getValueAsElement(i)))
        else:
            flat["fieldData"] = cls._flatten_fields(field_data)

    @staticmethod
    def _flatten_row(datum: Any) -> dict[str, Any]:
        """Flatten one historical ``fieldData`` row to ``{"date", "PX_LAST"?}``."""
        row: dict[str, Any] = {}
        raw_date = datum.getElement("date").getValueAsDatetime()
        if isinstance(raw_date, datetime):
            raw_date = raw_date.date()
        row["date"] = raw_date.isoformat()
        if datum.hasElement(_PX_LAST, True):
            # As a string to preserve the vendor's exact decimal text.
            row[_PX_LAST] = datum.getElementAsString(_PX_LAST)
        return row

    @staticmethod
    def _flatten_fields(field_data: Any) -> dict[str, Any]:
        """Flatten a reference ``fieldData`` element to a ``{field: str}`` map."""
        fields: dict[str, Any] = {}
        for i in range(field_data.numElements()):
            element = field_data.getElement(i)
            fields[str(element.name())] = element.getValueAsString()
        return fields
