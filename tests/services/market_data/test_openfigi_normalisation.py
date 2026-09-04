# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""OpenFIGI ISIN/ticker → FIGI resolution tests against a mocked transport.

No live network (``httpx_mock``). These pin the deterministic first-match
rule (ADR-0090 key-forming discipline), the keyless-vs-keyed header behaviour
now driven purely by the injected ``api_key`` parameter (ADR-0095 §5 —
credential-source-blindness; the module no longer reads the environment), and
the mapping of no-match / unsupported-scheme / HTTP failure onto the port error
types.

:func:`~services.market_data.normalisation.resolve_instrument` is the sibling
seam reading the *same* first entry more completely; its tests pin that the
FIGI stays required while ``name`` / ``currency`` are best-effort pre-fill —
``None`` is an honest absence, never a fabricated or coerced value.
"""

from __future__ import annotations

import pytest

from services.market_data.normalisation import (
    _API_KEY_HEADER,
    resolve_figi,
    resolve_instrument,
)
from services.market_data.provider import (
    IdentifierNotResolvableError,
    ProviderFetchError,
    UnsupportedCapabilityError,
)


class TestResolution:
    async def test_isin_maps_to_figi(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000B9XRY4", "name": "APPLE INC"}]}])
        figi = await resolve_figi("isin", "US0378331005")
        assert figi == "BBG000B9XRY4"

    async def test_ticker_maps_to_figi(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000BVPV84"}]}])
        figi = await resolve_figi("ticker", "AMZN")
        assert figi == "BBG000BVPV84"

    async def test_first_match_is_deterministic(self, httpx_mock) -> None:
        # Two candidates; the first-match rule takes data[0] deterministically.
        httpx_mock.add_response(
            json=[
                {
                    "data": [
                        {"figi": "BBG000FIRST0"},
                        {"figi": "BBG000SECOND"},
                    ]
                }
            ]
        )
        figi = await resolve_figi("isin", "US0378331005")
        assert figi == "BBG000FIRST0"

    async def test_request_body_uses_correct_id_type(self, httpx_mock) -> None:
        import json as _json

        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000B9XRY4"}]}])
        await resolve_figi("isin", "US0378331005")
        request = httpx_mock.get_requests()[0]
        body = _json.loads(request.content)
        assert body == [{"idType": "ID_ISIN", "idValue": "US0378331005"}]


class TestKeyHeader:
    async def test_keyless_sends_no_api_key_header(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000B9XRY4"}]}])
        await resolve_figi("isin", "US0378331005")
        request = httpx_mock.get_requests()[0]
        assert _API_KEY_HEADER not in request.headers

    async def test_keyed_sends_api_key_header(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000B9XRY4"}]}])
        await resolve_figi("isin", "US0378331005", api_key="secret-key")
        request = httpx_mock.get_requests()[0]
        assert request.headers.get(_API_KEY_HEADER) == "secret-key"

    async def test_env_key_is_ignored(self, httpx_mock, monkeypatch) -> None:
        # Credential-source-blindness (ADR-0095 §5): the module never reads the
        # environment. An ambient OPENFIGI_API_KEY must NOT reach the header —
        # only an injected `api_key` does (credential sourcing is the
        # CredentialResolver's job).
        monkeypatch.setenv("OPENFIGI_API_KEY", "env-key")
        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000B9XRY4"}]}])
        await resolve_figi("isin", "US0378331005")
        request = httpx_mock.get_requests()[0]
        assert _API_KEY_HEADER not in request.headers


class TestErrors:
    async def test_unsupported_scheme_raises(self) -> None:
        # A FIGI is already a FIGI; `internal` is a private namespace. Neither
        # is resolvable — and no HTTP call is made.
        with pytest.raises(UnsupportedCapabilityError):
            await resolve_figi("figi", "BBG000B9XRY4")

    async def test_provider_native_scheme_is_unsupported(self) -> None:
        # ADR-0096 §1: preqin / pitchbook identify illiquid funds with no
        # FIGI, so they are deliberately absent from _SCHEME_TO_ID_TYPE and
        # take the same unmapped-scheme path as figi / internal — no HTTP call
        # is made. This pins that no machine FIGI-resolution path exists for a
        # provider-native scheme (ADR-0096 §2).
        with pytest.raises(UnsupportedCapabilityError):
            await resolve_figi("preqin", "12345")
        with pytest.raises(UnsupportedCapabilityError):
            await resolve_figi("pitchbook", "PB-999")

    async def test_warning_maps_to_not_resolvable(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"warning": "No identifier found."}])
        with pytest.raises(IdentifierNotResolvableError):
            await resolve_figi("isin", "XX0000000000")

    async def test_empty_data_maps_to_not_resolvable(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"data": []}])
        with pytest.raises(IdentifierNotResolvableError):
            await resolve_figi("ticker", "NOPE")

    async def test_error_payload_maps_to_fetch_error(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"error": "Invalid idType."}])
        with pytest.raises(ProviderFetchError):
            await resolve_figi("cusip", "037833100")

    async def test_http_500_maps_to_fetch_error(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=500, text="boom")
        with pytest.raises(ProviderFetchError, match="HTTP 500"):
            await resolve_figi("isin", "US0378331005")


class TestResolveInstrument:
    """The pre-fill seam: FIGI required, name/currency opportunistic (W-4′)."""

    async def test_full_entry_populates_all_three_fields(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json=[
                {
                    "data": [
                        {"figi": "BBG000B9XRY4", "name": "APPLE INC", "currency": "USD"},
                    ]
                }
            ]
        )
        instrument = await resolve_instrument("isin", "US0378331005")
        assert instrument.figi == "BBG000B9XRY4"
        assert instrument.name == "APPLE INC"
        assert instrument.currency == "USD"

    async def test_name_only_entry_leaves_currency_none(self, httpx_mock) -> None:
        # The shape the recorded fixtures actually evidence: a mapping entry
        # with a name and no currency. `None` is the honest answer.
        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000B9XRY4", "name": "APPLE INC"}]}])
        instrument = await resolve_instrument("isin", "US0378331005")
        assert instrument.figi == "BBG000B9XRY4"
        assert instrument.name == "APPLE INC"
        assert instrument.currency is None

    async def test_bare_figi_entry_leaves_both_none(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000BVPV84"}]}])
        instrument = await resolve_instrument("ticker", "AMZN")
        assert instrument.figi == "BBG000BVPV84"
        assert instrument.name is None
        assert instrument.currency is None

    async def test_non_string_values_read_as_none(self, httpx_mock) -> None:
        # Defensive read: a non-string payload value is not coerced into a
        # string pre-fill — it reads as an absence (ADR-0090: no inference).
        httpx_mock.add_response(
            json=[{"data": [{"figi": "BBG000B9XRY4", "name": 42, "currency": ["USD"]}]}]
        )
        instrument = await resolve_instrument("isin", "US0378331005")
        assert instrument.figi == "BBG000B9XRY4"
        assert instrument.name is None
        assert instrument.currency is None

    async def test_null_values_read_as_none(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json=[{"data": [{"figi": "BBG000B9XRY4", "name": None, "currency": None}]}]
        )
        instrument = await resolve_instrument("isin", "US0378331005")
        assert instrument.name is None
        assert instrument.currency is None

    async def test_first_match_is_deterministic(self, httpx_mock) -> None:
        # Same first-match rule as resolve_figi: data[0] wins, whole entry.
        httpx_mock.add_response(
            json=[
                {
                    "data": [
                        {"figi": "BBG000FIRST0", "name": "FIRST CO", "currency": "EUR"},
                        {"figi": "BBG000SECOND", "name": "SECOND CO", "currency": "USD"},
                    ]
                }
            ]
        )
        instrument = await resolve_instrument("isin", "US0378331005")
        assert instrument.figi == "BBG000FIRST0"
        assert instrument.name == "FIRST CO"
        assert instrument.currency == "EUR"

    async def test_entry_without_figi_is_not_resolvable(self, httpx_mock) -> None:
        # A name alone is not a resolution: the FIGI is the required field.
        httpx_mock.add_response(json=[{"data": [{"name": "APPLE INC", "currency": "USD"}]}])
        with pytest.raises(IdentifierNotResolvableError):
            await resolve_instrument("isin", "US0378331005")

    async def test_warning_maps_to_not_resolvable(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"warning": "No identifier found."}])
        with pytest.raises(IdentifierNotResolvableError):
            await resolve_instrument("isin", "XX0000000000")

    async def test_unsupported_scheme_raises(self) -> None:
        # Same gate as resolve_figi, before any HTTP call is made.
        with pytest.raises(UnsupportedCapabilityError):
            await resolve_instrument("figi", "BBG000B9XRY4")

    async def test_error_payload_maps_to_fetch_error(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"error": "Invalid idType."}])
        with pytest.raises(ProviderFetchError):
            await resolve_instrument("cusip", "037833100")

    async def test_http_500_maps_to_fetch_error(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=500, text="boom")
        with pytest.raises(ProviderFetchError, match="HTTP 500"):
            await resolve_instrument("isin", "US0378331005")

    async def test_keyed_sends_api_key_header(self, httpx_mock) -> None:
        # Parity with the resolve_figi key tests: one request shape, one rule.
        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000B9XRY4"}]}])
        await resolve_instrument("isin", "US0378331005", api_key="secret-key")
        request = httpx_mock.get_requests()[0]
        assert request.headers.get(_API_KEY_HEADER) == "secret-key"

    async def test_keyless_sends_no_api_key_header(self, httpx_mock) -> None:
        httpx_mock.add_response(json=[{"data": [{"figi": "BBG000B9XRY4"}]}])
        await resolve_instrument("isin", "US0378331005")
        request = httpx_mock.get_requests()[0]
        assert _API_KEY_HEADER not in request.headers
