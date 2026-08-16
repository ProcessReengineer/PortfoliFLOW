# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Bloomberg adapter tests against a fake gateway (ADR-0091, #036 §6).

The whole point of the gateway seam is that these run with **no `blpapi` and no
network** (the SDK is not on public PyPI): a synchronous fake gateway supplies
canned, already-flattened responses and records what it was asked, so nothing
here mocks a ``blpapi`` object. The tests pin the in-adapter normalisation
(property 3): ``PX_LAST`` history → ``nav_price`` points with holiday rows
skipped, currency from the ``CRNCY`` reference field (never guessed), rule-based
security-topic formation, the single ``asyncio.to_thread`` bridge, the error
mapping onto the port error types, the lazy-``blpapi`` configuration error, the
provider-blindness golden extension, and the ``enabled``-flag routing.
"""

from __future__ import annotations

import dataclasses
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from services.market_data.adapters.bloomberg import (
    BloombergAdapter,
    BloombergGateway,
    BloombergGatewayError,
    BlpapiDesktopGateway,
)
from services.market_data.adapters.synthetic import SyntheticAdapter
from services.market_data.dto import (
    DateWindow,
    NormalizedIdentifier,
    SeriesKind,
    SeriesPoint,
)
from services.market_data.factory import build_adapter, load_capability_matrix
from services.market_data.provider import (
    IdentifierNotResolvableError,
    MarketDataConfigurationError,
    MarketDataProvider,
    ProviderFetchError,
    UnsupportedCapabilityError,
)

_FIGI = NormalizedIdentifier("figi", "BBG000B9XRY4")
_ISIN = NormalizedIdentifier("isin", "US0378331005")
_WINDOW = DateWindow(date(2026, 1, 1), date(2026, 1, 31))

_HISTORICAL_OP = "HistoricalDataRequest"
_REFERENCE_OP = "ReferenceDataRequest"


def _ok_history() -> dict:
    """A valid PX_LAST history: two real points plus a date-only holiday row."""
    return {
        "securityError": None,
        "responseError": None,
        "fieldExceptions": [],
        "fieldData": [
            {"date": "2026-01-02", "PX_LAST": "185.5"},
            {"date": "2026-01-05"},  # holiday / halted day — no PX_LAST → skip.
            {"date": "2026-01-06", "PX_LAST": "188.0"},
        ],
    }


def _ok_currency(code: str = "USD") -> dict:
    return {
        "securityError": None,
        "responseError": None,
        "fieldExceptions": [],
        "fieldData": {"CRNCY": code},
    }


class _FakeGateway:
    """A synchronous fake :class:`BloombergGateway`.

    Dispatches by ``operation`` to a canned historical / reference response,
    records every request it received and the OS thread each ran on, and can be
    told to raise instead (to exercise the gateway-failure mapping).
    """

    def __init__(
        self,
        *,
        historical: dict | None = None,
        reference: dict | None = None,
        raise_error: Exception | None = None,
    ) -> None:
        self._historical = historical
        self._reference = reference
        self._raise_error = raise_error
        self.requests: list[dict] = []
        self.threads: list[int] = []

    def execute(self, request: dict) -> dict:
        self.requests.append(request)
        self.threads.append(threading.get_ident())
        if self._raise_error is not None:
            raise self._raise_error
        if request["operation"] == _HISTORICAL_OP:
            assert self._historical is not None
            return self._historical
        assert self._reference is not None
        return self._reference


def _adapter(**kwargs) -> tuple[BloombergAdapter, _FakeGateway]:
    gateway = _FakeGateway(**kwargs)
    return BloombergAdapter(gateway=gateway), gateway


class TestGatewaySeam:
    def test_fake_satisfies_the_protocol(self) -> None:
        # runtime_checkable: the fake is a structural BloombergGateway.
        assert isinstance(_FakeGateway(), BloombergGateway)

    def test_adapter_is_a_market_data_provider(self) -> None:
        adapter, _ = _adapter()
        assert isinstance(adapter, MarketDataProvider)


class TestNavPriceHappyPath:
    async def test_maps_px_last_skips_holiday_and_reads_currency(self) -> None:
        adapter, gateway = _adapter(historical=_ok_history(), reference=_ok_currency("USD"))
        series = await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)

        assert series.provider == "bloomberg"
        assert series.kind is SeriesKind.NAV_PRICE
        assert series.currency == "USD"
        # The date-only 2026-01-05 holiday row is dropped; two points remain,
        # ordered ascending, exact Decimals.
        assert series.points == (
            SeriesPoint(date(2026, 1, 2), Decimal("185.5")),
            SeriesPoint(date(2026, 1, 6), Decimal("188.0")),
        )
        # Two gateway calls: the PX_LAST history and the CRNCY reference.
        ops = [r["operation"] for r in gateway.requests]
        assert ops == [_HISTORICAL_OP, _REFERENCE_OP]

    async def test_points_outside_window_filtered(self) -> None:
        adapter, _ = _adapter(historical=_ok_history(), reference=_ok_currency())
        narrow = DateWindow(date(2026, 1, 1), date(2026, 1, 2))
        series = await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, narrow)
        assert [p.as_of_date for p in series.points] == [date(2026, 1, 2)]

    async def test_historical_request_uses_bbg_yyyymmdd_inclusive_window(
        self,
    ) -> None:
        adapter, gateway = _adapter(historical=_ok_history(), reference=_ok_currency())
        await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)
        hist = gateway.requests[0]
        assert hist["fields"] == ["PX_LAST"]
        assert hist["options"]["periodicitySelection"] == "DAILY"
        assert hist["options"]["startDate"] == "20260101"
        assert hist["options"]["endDate"] == "20260131"


class TestTopicFormation:
    async def test_figi_forms_bbgid_topic(self) -> None:
        adapter, gateway = _adapter(historical=_ok_history(), reference=_ok_currency())
        await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)
        assert gateway.requests[0]["security"] == "/bbgid/BBG000B9XRY4"
        assert gateway.requests[1]["security"] == "/bbgid/BBG000B9XRY4"

    async def test_isin_forms_isin_topic(self) -> None:
        adapter, gateway = _adapter(historical=_ok_history(), reference=_ok_currency())
        await adapter.fetch_series(_ISIN, SeriesKind.NAV_PRICE, _WINDOW)
        assert gateway.requests[0]["security"] == "/isin/US0378331005"

    @pytest.mark.parametrize("scheme", ["ticker", "internal", "preqin"])
    async def test_unsupported_scheme_raises_without_any_gateway_call(self, scheme: str) -> None:
        adapter, gateway = _adapter()
        with pytest.raises(UnsupportedCapabilityError):
            await adapter.fetch_series(
                NormalizedIdentifier(scheme, "X"), SeriesKind.NAV_PRICE, _WINDOW
            )
        assert gateway.requests == []

    async def test_unsupported_kind_raises_without_any_gateway_call(self) -> None:
        adapter, gateway = _adapter()
        with pytest.raises(UnsupportedCapabilityError):
            await adapter.fetch_series(_FIGI, SeriesKind.DIVIDEND, _WINDOW)
        assert gateway.requests == []


class TestErrorMapping:
    async def test_security_error_maps_to_not_resolvable(self) -> None:
        bad = {
            "securityError": {"message": "Unknown/Invalid security"},
            "responseError": None,
            "fieldExceptions": [],
            "fieldData": [],
        }
        adapter, gateway = _adapter(historical=bad)
        with pytest.raises(IdentifierNotResolvableError):
            await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)
        # Failed on the first (historical) call — no currency request made.
        assert [r["operation"] for r in gateway.requests] == [_HISTORICAL_OP]

    async def test_response_error_maps_to_fetch_error(self) -> None:
        bad = {
            "securityError": None,
            "responseError": {"message": "service unavailable"},
            "fieldExceptions": [],
            "fieldData": [],
        }
        adapter, _ = _adapter(historical=bad)
        with pytest.raises(ProviderFetchError):
            await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)

    async def test_field_exception_maps_to_fetch_error(self) -> None:
        bad = {
            "securityError": None,
            "responseError": None,
            "fieldExceptions": [{"fieldId": "PX_LAST", "message": "bad field"}],
            "fieldData": [],
        }
        adapter, _ = _adapter(historical=bad)
        with pytest.raises(ProviderFetchError):
            await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)

    async def test_empty_currency_maps_to_fetch_error(self) -> None:
        empty_ccy = {
            "securityError": None,
            "responseError": None,
            "fieldExceptions": [],
            "fieldData": {"CRNCY": ""},
        }
        adapter, _ = _adapter(historical=_ok_history(), reference=empty_ccy)
        with pytest.raises(ProviderFetchError, match="CRNCY"):
            await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)

    async def test_missing_currency_field_maps_to_fetch_error(self) -> None:
        no_ccy = {
            "securityError": None,
            "responseError": None,
            "fieldExceptions": [],
            "fieldData": {},
        }
        adapter, _ = _adapter(historical=_ok_history(), reference=no_ccy)
        with pytest.raises(ProviderFetchError, match="CRNCY"):
            await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)

    async def test_gateway_session_failure_maps_to_fetch_error(self) -> None:
        # A session/service failure the gateway surfaces as its private error
        # type must cross the port as ProviderFetchError, never as itself.
        adapter, _ = _adapter(raise_error=BloombergGatewayError("session start failed"))
        with pytest.raises(ProviderFetchError, match="gateway failure"):
            await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)


class TestBridge:
    async def test_gateway_runs_off_the_event_loop_thread(self) -> None:
        # asyncio.to_thread proof: the synchronous gateway.execute ran on a
        # worker thread, not the event-loop (test) thread.
        adapter, gateway = _adapter(historical=_ok_history(), reference=_ok_currency())
        main_thread = threading.get_ident()
        await adapter.fetch_series(_FIGI, SeriesKind.NAV_PRICE, _WINDOW)
        assert gateway.threads, "gateway was never called"
        assert all(tid != main_thread for tid in gateway.threads)


class TestLazyDependency:
    def test_import_and_build_succeed_without_blpapi(self) -> None:
        # Importing this module already happened at collection; building the
        # factory adapter must also succeed on a machine with no blpapi.
        adapter = build_adapter("bloomberg")
        assert isinstance(adapter, MarketDataProvider)

    def test_real_gateway_fetch_raises_configuration_error(self) -> None:
        # The real gateway lazily imports blpapi inside execute(); on this
        # machine (no blpapi) that surfaces as MarketDataConfigurationError with
        # the Bloomberg pip-index install hint.
        gateway = BlpapiDesktopGateway(host="localhost", port=8194)
        request = {
            "service": "//blp/refdata",
            "operation": _REFERENCE_OP,
            "security": "/isin/US0378331005",
            "fields": ["CRNCY"],
            "options": {},
        }
        with pytest.raises(MarketDataConfigurationError) as excinfo:
            gateway.execute(request)
        assert "blpapi.bloomberg.com" in str(excinfo.value)


class TestProviderBlindnessGolden:
    async def test_bloomberg_vs_synthetic_indistinguishable_except_provider(
        self, tmp_path: Path
    ) -> None:
        # The same logical nav_price series from the fake-gateway Bloomberg
        # adapter and the synthetic adapter must be equal in every field except
        # `provider` — mirroring the DTO golden test's mechanism (property 1).
        ident = _FIGI
        window = DateWindow(date(2026, 1, 1), date(2026, 1, 6))

        history = {
            "securityError": None,
            "responseError": None,
            "fieldExceptions": [],
            "fieldData": [
                {"date": "2026-01-02", "PX_LAST": "185.5"},
                {"date": "2026-01-06", "PX_LAST": "188.0"},
            ],
        }
        bloomberg = BloombergAdapter(
            gateway=_FakeGateway(historical=history, reference=_ok_currency("USD"))
        )
        bloomberg_series = await bloomberg.fetch_series(ident, SeriesKind.NAV_PRICE, window)

        fixture = tmp_path / "synthetic.json"
        fixture.write_text(
            '{"BBG000B9XRY4": {"nav_price": [["2026-01-02", "185.5"], ["2026-01-06", "188.0"]]}}',
            encoding="utf-8",
        )
        synthetic_series = await SyntheticAdapter(fixture, currency="USD").fetch_series(
            ident, SeriesKind.NAV_PRICE, window
        )

        assert bloomberg_series.provider == "bloomberg"
        assert synthetic_series.provider == "synthetic"

        neutral_bloomberg = dataclasses.replace(bloomberg_series, provider="_")
        neutral_synthetic = dataclasses.replace(synthetic_series, provider="_")
        assert neutral_bloomberg == neutral_synthetic


# ---------------------------------------------------------------------------
# Factory / matrix routing with the `enabled` flag (§6.7 / §7.7)
# ---------------------------------------------------------------------------

_YAHOO_ENTRY = (
    "  - name: yahoo\n    priority: 100\n    schemes: [ticker]\n    kinds: [nav_price, dividend]\n"
)
_CREDENTIALS = "credentials:\n  yahoo: none\n  bloomberg: none\n"


def _matrix_with_bloomberg(tmp_path: Path, *, enabled: bool):
    """Write and load a yahoo + bloomberg matrix (no synthetic) with a toggle."""
    yaml_text = (
        "providers:\n"
        + _YAHOO_ENTRY
        + "  - name: bloomberg\n"
        + "    priority: 200\n"
        + f"    enabled: {'true' if enabled else 'false'}\n"
        + "    schemes: [figi, isin]\n"
        + "    kinds: [nav_price]\n"
        + _CREDENTIALS
    )
    path = tmp_path / "m.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return load_capability_matrix(path)


class TestFactoryRoutingWithEnabled:
    def test_disabled_bloomberg_is_never_routed(self, tmp_path: Path) -> None:
        # With bloomberg disabled and no synthetic fallback, a figi/nav_price
        # request is genuinely uncovered — proving no accidental routing to a
        # disabled provider.
        matrix = _matrix_with_bloomberg(tmp_path, enabled=False)
        assert [p.name for p in matrix.providers] == ["yahoo"]
        with pytest.raises(UnsupportedCapabilityError):
            matrix.resolve("figi", SeriesKind.NAV_PRICE)

    def test_enabled_bloomberg_routes_figi_and_isin(self, tmp_path: Path) -> None:
        matrix = _matrix_with_bloomberg(tmp_path, enabled=True)
        assert matrix.resolve("figi", SeriesKind.NAV_PRICE).name == "bloomberg"
        assert matrix.resolve("isin", SeriesKind.NAV_PRICE).name == "bloomberg"

    def test_enabling_bloomberg_leaves_ticker_on_yahoo(self, tmp_path: Path) -> None:
        matrix = _matrix_with_bloomberg(tmp_path, enabled=True)
        assert matrix.resolve("ticker", SeriesKind.NAV_PRICE).name == "yahoo"

    def test_shipped_matrix_ships_bloomberg_disabled(self) -> None:
        # The shipped matrix declares bloomberg but disables it, so it is absent
        # from routing and its policy is still parseable.
        matrix = load_capability_matrix()
        assert "bloomberg" not in {p.name for p in matrix.providers}
        assert matrix.credential_policy("bloomberg").requires is False
