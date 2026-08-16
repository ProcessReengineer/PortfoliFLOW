# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""OpenRouter model-catalog client — the live ``GET /models`` list.

A small, DB-free and FastAPI-free reader for the OpenAI-compatible
``/models`` endpoint of whatever base URL a tenant has configured. Its one
consumer is the Providers & Credentials surface (ADR-0112 §6), which offers
the result as a ``<datalist>`` autocomplete behind the OpenRouter *model*
and *Watch Desk model* inputs.

Three disciplines this module exists to keep:

- **Server-side only.** The catalog is fetched here, with a key resolved
  through :class:`~services.investments.credential_resolver.CredentialResolver`,
  so the credential never reaches a browser. The endpoint is also usable
  keyless — OpenRouter serves ``/models`` publicly — which is why
  ``api_key`` is optional rather than required.
- **Per-call client lifecycle.** With no client injected, a fresh
  ``httpx.AsyncClient`` is built inside the call and closed in a ``finally``
  — an ``httpx.AsyncClient`` is bound to the loop it was created on, so it
  must not be cached across calls or threads (the discipline
  :class:`~services.voice.openai_provider.OpenAIVoiceProvider` follows for
  the same reason). ``pytest-httpx`` intercepts at the transport layer, so
  this mocks transparently.
- **Operator-safe failure.** Every failure — transport, non-2xx, or an
  unreadable body — becomes one :class:`CatalogFetchError` whose message is
  built *here*, from a status code or an exception class name. Neither the
  API key nor the base URL is ever interpolated into it, so the message is
  safe to render inline and to log. A base URL can carry credentials in its
  userinfo part; keeping it out of the message keeps that impossible by
  construction.

There is no fallback and no cache: a failed fetch raises, and the caller
renders the failure rather than an empty list that would read as "this
endpoint offers no models".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Total per-request budget, in seconds. One attempt, no retries: the fetch
#: is a click, and an operator would rather retry than wait.
_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class CatalogModel:
    """One entry of the provider's model catalog.

    Attributes:
        id: The model identifier, e.g. ``"anthropic/claude-sonnet-4-6"``.
            This is the value written into the field.
        name: The human-readable display name, falling back to :attr:`id`
            when the provider states none.
    """

    id: str
    name: str


class CatalogFetchError(Exception):
    """The model list could not be fetched or read.

    Carries a message written in this module and therefore safe to render
    to an operator and to log: it names a status code or an exception
    class, never the API key and never the base URL.
    """


async def fetch_models(
    base_url: str,
    api_key: str | None,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[CatalogModel]:
    """Fetch the model catalog from an OpenAI-compatible endpoint.

    Calls ``GET {base_url}/models`` and parses the OpenRouter/OpenAI shape
    ``{"data": [{"id": ..., "name": ...}, ...]}``. Entries the shape does
    not fit — a non-object entry, or one with no usable ``id`` — are
    skipped rather than failing the whole list; only a body that carries no
    list at all is a failure.

    Args:
        base_url: The endpoint root, with or without a trailing slash
            (e.g. ``"https://openrouter.ai/api/v1"``).
        api_key: The bearer token to authenticate with, or ``None`` to call
            the endpoint keyless. ``/models`` is public at OpenRouter, so a
            missing credential is a valid way to call this.
        client: An ``httpx.AsyncClient`` to reuse. When ``None`` (the
            default) one is built for this call and closed before returning.
            An injected client is left open — it belongs to the caller.

    Returns:
        The catalog, de-duplicated on :attr:`CatalogModel.id` (first
        occurrence wins) and sorted by it. May be empty if the endpoint
        genuinely offers nothing.

    Raises:
        CatalogFetchError: On a transport failure, a timeout, a non-2xx
            status, a body that is not JSON, or a body carrying no model
            list.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    if client is None:
        http = httpx.AsyncClient()
        owned = True
    else:
        http = client
        owned = False
    try:
        response = await http.get(url, headers=headers, timeout=_TIMEOUT_SECONDS)
    except httpx.TimeoutException as exc:
        raise CatalogFetchError(
            f"the model endpoint did not answer within {_TIMEOUT_SECONDS:.0f} seconds."
        ) from exc
    except httpx.HTTPError as exc:
        raise CatalogFetchError(
            f"the model endpoint could not be reached ({type(exc).__name__})."
        ) from exc
    finally:
        if owned:
            await http.aclose()

    # A non-streaming ``get`` has already read the body, so parsing after the
    # client is closed is safe.
    if not 200 <= response.status_code < 300:
        raise CatalogFetchError(f"the model endpoint answered HTTP {response.status_code}.")

    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise CatalogFetchError(
            "the model endpoint answered with a body that is not JSON."
        ) from exc

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise CatalogFetchError("the model endpoint answered with a body carrying no model list.")

    by_id: dict[str, CatalogModel] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        identifier = raw_id.strip()
        if identifier in by_id:
            continue
        raw_name = entry.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else identifier
        by_id[identifier] = CatalogModel(id=identifier, name=name)

    models = [by_id[identifier] for identifier in sorted(by_id)]
    logger.debug("openrouter catalog: %d model(s) parsed (keyed=%s).", len(models), bool(api_key))
    return models


__all__ = ["CatalogFetchError", "CatalogModel", "fetch_models"]
