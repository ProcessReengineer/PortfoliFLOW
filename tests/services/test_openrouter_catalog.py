# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""OpenRouter model-catalog client tests against a mocked transport.

No live network: every request is faked with ``httpx_mock``, the codebase's
established HTTP-faking idiom (cf. ``tests/services/market_data/test_yahoo_adapter``).

Four properties the module exists to hold:

* the OpenRouter ``{"data": [...]}`` shape parses, sorts by id and
  de-duplicates on it, with ``name`` falling back to the id;
* every failure — non-2xx, timeout, transport, unreadable body — becomes
  one :class:`CatalogFetchError` and never a silently empty list;
* the ``Authorization`` header is present exactly when a key was given, so
  the public keyless call really is keyless;
* the key never appears in the exception message, which is rendered inline
  to an operator and written to the log.
"""

from __future__ import annotations

import httpx
import pytest

from services.openrouter_catalog import CatalogFetchError, fetch_models

_BASE_URL = "https://openrouter.ai/api/v1"
_MODELS_URL = f"{_BASE_URL}/models"

#: Long enough to be recognisable in a message if it ever leaked into one.
_API_KEY = "sk-or-v1-never-render-this-anywhere-4242"


def _payload(*entries: dict[str, object]) -> dict[str, object]:
    return {"data": list(entries)}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


async def test_parses_sorts_and_dedupes_on_id(httpx_mock) -> None:
    httpx_mock.add_response(
        json=_payload(
            {"id": "openai/gpt-5", "name": "GPT-5"},
            {"id": "anthropic/claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
            # A duplicate id: first occurrence wins, so the second name is lost.
            {"id": "openai/gpt-5", "name": "GPT-5 (mirror)"},
            {"id": "anthropic/claude-opus-4-8", "name": "Claude Opus 4.8"},
        )
    )

    models = await fetch_models(_BASE_URL, None)

    assert [m.id for m in models] == [
        "anthropic/claude-opus-4-8",
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-5",
    ]
    assert [m.name for m in models] == ["Claude Opus 4.8", "Claude Sonnet 4.6", "GPT-5"]


@pytest.mark.parametrize(
    "entry", [{"id": "x/y"}, {"id": "x/y", "name": None}, {"id": "x/y", "name": "   "}]
)
async def test_missing_name_falls_back_to_id(httpx_mock, entry: dict[str, object]) -> None:
    httpx_mock.add_response(json=_payload(entry))

    models = await fetch_models(_BASE_URL, None)

    assert len(models) == 1
    assert models[0].id == "x/y"
    assert models[0].name == "x/y"


async def test_unusable_entries_are_skipped_not_fatal(httpx_mock) -> None:
    # A non-object entry and an id-less one are noise in an otherwise good
    # list; dropping them beats failing the whole fetch over them.
    httpx_mock.add_response(
        json=_payload(
            {"id": "good/one", "name": "Good"},
            {"name": "no id at all"},
            {"id": "   "},
        )
    )

    models = await fetch_models(_BASE_URL, None)

    assert [m.id for m in models] == ["good/one"]


async def test_empty_catalog_is_an_empty_list_not_an_error(httpx_mock) -> None:
    httpx_mock.add_response(json=_payload())

    assert await fetch_models(_BASE_URL, None) == []


async def test_trailing_slash_on_base_url_does_not_double(httpx_mock) -> None:
    httpx_mock.add_response(json=_payload({"id": "a/b"}))

    await fetch_models(f"{_BASE_URL}/", None)

    assert str(httpx_mock.get_requests()[0].url) == _MODELS_URL


# ---------------------------------------------------------------------------
# Failure mapping — no silent fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [401, 403, 404, 500, 502])
async def test_non_2xx_raises(httpx_mock, status_code: int) -> None:
    httpx_mock.add_response(status_code=status_code, text="nope")

    with pytest.raises(CatalogFetchError, match=f"HTTP {status_code}"):
        await fetch_models(_BASE_URL, None)


async def test_timeout_raises(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("read timed out"))

    with pytest.raises(CatalogFetchError, match="did not answer within"):
        await fetch_models(_BASE_URL, None)


async def test_transport_error_raises(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("name or service not known"))

    with pytest.raises(CatalogFetchError, match="could not be reached"):
        await fetch_models(_BASE_URL, None)


async def test_non_json_body_raises(httpx_mock) -> None:
    httpx_mock.add_response(text="<html>a proxy login page</html>")

    with pytest.raises(CatalogFetchError, match="not JSON"):
        await fetch_models(_BASE_URL, None)


@pytest.mark.parametrize("body", [{"models": []}, {"data": {"a": 1}}, [1, 2, 3]])
async def test_body_without_a_model_list_raises(httpx_mock, body: object) -> None:
    httpx_mock.add_response(json=body)

    with pytest.raises(CatalogFetchError, match="no model list"):
        await fetch_models(_BASE_URL, None)


# ---------------------------------------------------------------------------
# The key: sent when given, never rendered
# ---------------------------------------------------------------------------


async def test_authorization_header_present_when_key_given(httpx_mock) -> None:
    httpx_mock.add_response(json=_payload({"id": "a/b"}))

    await fetch_models(_BASE_URL, _API_KEY)

    assert httpx_mock.get_requests()[0].headers["Authorization"] == f"Bearer {_API_KEY}"


async def test_authorization_header_absent_when_no_key(httpx_mock) -> None:
    httpx_mock.add_response(json=_payload({"id": "a/b"}))

    await fetch_models(_BASE_URL, None)

    assert "Authorization" not in httpx_mock.get_requests()[0].headers


@pytest.mark.parametrize(
    "arrange",
    [
        pytest.param(
            lambda mock: mock.add_response(status_code=401, json={"error": "bad key"}),
            id="non-2xx",
        ),
        pytest.param(
            lambda mock: mock.add_exception(httpx.ReadTimeout("timed out")),
            id="timeout",
        ),
        pytest.param(
            lambda mock: mock.add_exception(httpx.ConnectError("refused")),
            id="transport",
        ),
        pytest.param(lambda mock: mock.add_response(text="not json"), id="unparseable"),
    ],
)
async def test_key_never_appears_in_the_error_message(httpx_mock, arrange) -> None:
    # The message is rendered inline to an operator and written to the log,
    # so it is built from a status code or an exception class name — never
    # from the credential, and never from the URL that could carry one.
    arrange(httpx_mock)

    with pytest.raises(CatalogFetchError) as excinfo:
        await fetch_models(f"https://user:{_API_KEY}@example.test/v1", _API_KEY)

    message = str(excinfo.value)
    assert _API_KEY not in message
    assert "example.test" not in message


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------


async def test_injected_client_is_used_and_left_open(httpx_mock) -> None:
    httpx_mock.add_response(json=_payload({"id": "a/b"}))

    async with httpx.AsyncClient(headers={"X-Test": "injected"}) as client:
        models = await fetch_models(_BASE_URL, None, client=client)
        # Still usable after the call: the caller owns its lifetime.
        assert not client.is_closed

    assert [m.id for m in models] == ["a/b"]
    assert httpx_mock.get_requests()[0].headers["X-Test"] == "injected"
