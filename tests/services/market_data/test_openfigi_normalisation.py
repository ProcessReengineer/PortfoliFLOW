# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""OpenFIGI ISIN/ticker → FIGI resolution tests against a mocked transport.

No live network (``httpx_mock``). These pin the deterministic first-match
rule (ADR-0090 key-forming discipline), the keyless-vs-keyed header behaviour
now driven purely by the injected ``api_key`` parameter (ADR-0095 §5 —
credential-source-blindness; the module no longer reads the environment), and
the mapping of no-match / unsupported-scheme / HTTP failure onto the port error
types.
"""

from __future__ import annotations

import pytest

from services.market_data.normalisation import (
    _API_KEY_HEADER,
    resolve_figi,
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
