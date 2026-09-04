# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Deterministic OpenFIGI identifier normalisation: ISIN/ticker → FIGI (ADR-0090).

FIGI is the stable internal join-key the market-data adapters prefer for
provider calls (ADR-0090 §"FIGI is the internal provider join-key"). This
module resolves a human-readable identifier (ISIN, ticker, CUSIP) to a FIGI
via OpenFIGI's free ``/v3/mapping`` endpoint, natively async over ``httpx``.

The resolution is **deterministic and rule-based**, honouring the project-wide
key-forming discipline (mirrored from Irene's subject-key rule, ADR-0087, and
guarded in spirit by ``tests/regression/test_irene_key_forming_pure.py``): the
same input forms the same request, and the **first** ``data`` entry OpenFIGI
returns is taken as the mapping. No LLM, no heuristic, no inference — a failed
resolution is a recorded gap (:class:`IdentifierNotResolvableError`), never a
fabricated FIGI.

This module performs **pure network mapping only — no persistence**. Writing
resolved ``figi`` rows (with ``source='openfigi'``) into
``investment_identifiers`` is a later slice's service-layer concern; nothing
here touches a repository.

An API key is **optional and injected by the caller**: when an ``api_key`` is
passed it is sent in the ``X-OPENFIGI-APIKEY`` header (higher rate limit);
without one the endpoint works keyless at the lower anonymous limit. This module
never reads the environment — credential sourcing flows through the
:class:`~services.investments.credential_resolver.CredentialResolver`
(ADR-0095 §1), keeping the adapter credential-source-blind.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from services.market_data.provider import (
    IdentifierNotResolvableError,
    ProviderFetchError,
    UnsupportedCapabilityError,
)

_MAPPING_URL = "https://api.openfigi.com/v3/mapping"
_API_KEY_HEADER = "X-OPENFIGI-APIKEY"
_DEFAULT_TIMEOUT = 8.0

# Identifier scheme (ADR-0090) → OpenFIGI ``idType``. Only the human-readable
# schemes are resolvable to a FIGI; ``figi`` is already a FIGI and ``internal``
# is a private operator namespace with no external meaning. The provider-native
# private-markets schemes ``preqin`` / ``pitchbook`` (ADR-0096 §1) are
# deliberately absent: they identify illiquid funds with no FIGI, so there is
# no ID type to map them onto — an unmapped scheme raises
# UnsupportedCapabilityError below, exactly as ``figi`` / ``internal`` do.
_SCHEME_TO_ID_TYPE: dict[str, str] = {
    "isin": "ID_ISIN",
    "ticker": "TICKER",
    "cusip": "ID_CUSIP",
}


@dataclass(frozen=True)
class ResolvedInstrument:
    """What one OpenFIGI mapping entry says about an instrument.

    The FIGI **is** the resolution: it is required, and it is read under the
    same deterministic first-match rule as :func:`resolve_figi` — an entry
    without one is not a resolution (:class:`IdentifierNotResolvableError`).

    The other two fields are *pre-fill*, not resolution. ``name`` is evidenced
    by OpenFIGI's mapping entries; ``currency`` is read **opportunistically** —
    the recorded fixtures do not evidence the field at all, so no caller may
    require it and ``None`` is an honest absence, never a fabricated value.
    Both are read defensively: a non-string payload value reads as ``None``
    rather than being coerced. Strings pass through untrimmed — ISO-4217 shape
    validation is the consuming route's job (the ``_validate_currency``
    precedent in ``web/routes/investments.py``), and the consumer owns the
    result: the operator may overwrite either field.

    Attributes:
        figi: The resolved FIGI. Always present.
        name: The instrument name as OpenFIGI stated it, or ``None``.
        currency: The instrument currency as OpenFIGI stated it, or ``None``.
    """

    figi: str
    name: str | None
    currency: str | None


async def resolve_figi(
    scheme: str,
    value: str,
    *,
    api_key: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """Resolve ``(scheme, value)`` to a FIGI via OpenFIGI.

    Deterministic: the input maps to exactly one request, and the first
    ``data`` entry in the response is returned (the explicit first-match rule).

    Args:
        scheme: The source scheme — ``isin``, ``ticker``, or ``cusip``.
        value: The identifier value (e.g. an ISIN or ticker symbol).
        api_key: Optional OpenFIGI API key, injected by the caller (resolved via
            the :class:`~services.investments.credential_resolver.CredentialResolver`,
            ADR-0095). When ``None`` the call is made keyless at the lower rate
            limit; this module never reads the environment.
        timeout: Per-request timeout in seconds.

    Returns:
        The resolved FIGI string.

    Raises:
        UnsupportedCapabilityError: If ``scheme`` is not resolvable to a FIGI
            (``figi`` / ``internal`` / any unknown scheme).
        IdentifierNotResolvableError: If OpenFIGI returns no mapping (a
            ``warning`` payload or an empty ``data`` list).
        ProviderFetchError: On timeout, transport failure, a non-2xx status, or
            an OpenFIGI ``error`` payload.
    """
    payload = await _post_mapping_job(scheme, value, api_key=api_key, timeout=timeout)
    return _first_figi(payload, scheme, value)


async def resolve_instrument(
    scheme: str,
    value: str,
    *,
    api_key: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ResolvedInstrument:
    """Resolve ``(scheme, value)`` to a :class:`ResolvedInstrument` via OpenFIGI.

    The sibling of :func:`resolve_figi`: the *same* request, the *same*
    first-match rule and the *same* error mapping — it simply reads the one
    first ``data`` entry more completely. The FIGI is the resolution and is
    required; ``name`` and ``currency`` are best-effort pre-fill, ``None`` when
    OpenFIGI does not state them (see :class:`ResolvedInstrument`).

    Args:
        scheme: The source scheme — ``isin``, ``ticker``, or ``cusip``.
        value: The identifier value (e.g. an ISIN or ticker symbol).
        api_key: Optional OpenFIGI API key, injected by the caller (resolved via
            the :class:`~services.investments.credential_resolver.CredentialResolver`,
            ADR-0095). When ``None`` the call is made keyless at the lower rate
            limit; this module never reads the environment.
        timeout: Per-request timeout in seconds.

    Returns:
        The :class:`ResolvedInstrument` built from the first mapping entry.

    Raises:
        UnsupportedCapabilityError: If ``scheme`` is not resolvable to a FIGI
            (``figi`` / ``internal`` / any unknown scheme).
        IdentifierNotResolvableError: If OpenFIGI returns no mapping (a
            ``warning`` payload or an empty ``data`` list), or the first entry
            carries no FIGI.
        ProviderFetchError: On timeout, transport failure, a non-2xx status, or
            an OpenFIGI ``error`` payload.
    """
    payload = await _post_mapping_job(scheme, value, api_key=api_key, timeout=timeout)
    entry = _first_entry(payload, scheme, value)
    return ResolvedInstrument(
        figi=_require_figi(entry, scheme, value),
        name=_optional_str(entry, "name"),
        currency=_optional_str(entry, "currency"),
    )


async def _post_mapping_job(
    scheme: str,
    value: str,
    *,
    api_key: str | None,
    timeout: float,
) -> Any:
    """Post the single mapping job for ``(scheme, value)`` and return its payload.

    The module's one request shape, shared by both public functions so the
    determinism doctrine holds across them: one input, one gate, one job, one
    key-header rule.

    Returns:
        The decoded JSON payload (shape-checked by the extractors below).

    Raises:
        UnsupportedCapabilityError: If ``scheme`` is not resolvable to a FIGI.
        ProviderFetchError: On timeout, transport failure, or a non-2xx status.
    """
    id_type = _SCHEME_TO_ID_TYPE.get(scheme)
    if id_type is None:
        raise UnsupportedCapabilityError(
            f"Scheme {scheme!r} is not resolvable to a FIGI; resolvable "
            f"schemes are {sorted(_SCHEME_TO_ID_TYPE)}."
        )

    request_body = [{"idType": id_type, "idValue": value}]
    headers = {"Content-Type": "application/json"}
    # The key is injected by the caller (credential resolver, ADR-0095); this
    # module never reads the environment. Header sent iff a key was passed.
    if api_key:
        headers[_API_KEY_HEADER] = api_key

    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            response = await client.post(_MAPPING_URL, json=request_body)
    except httpx.TimeoutException as exc:
        raise ProviderFetchError(f"Timeout resolving {scheme}:{value} via OpenFIGI: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ProviderFetchError(
            f"HTTP error resolving {scheme}:{value} via OpenFIGI: {exc}"
        ) from exc

    if response.status_code >= 400:
        raise ProviderFetchError(
            f"OpenFIGI returned HTTP {response.status_code} for {scheme}:{value}."
        )

    return response.json()


def _first_entry(payload: Any, scheme: str, value: str) -> Mapping[str, Any]:
    """Extract the first mapping entry from an OpenFIGI mapping response.

    OpenFIGI returns a list aligned with the request jobs. For our single job
    the first element is either ``{"data": [ {...}, ... ]}`` (a hit),
    ``{"warning": "..."}`` (no match), or ``{"error": "..."}`` (a job error).
    The **first** ``data`` entry is the mapping (the explicit first-match rule).

    Raises:
        IdentifierNotResolvableError: On a warning, empty ``data``, or a first
            entry that is not an object (an entry with no readable FIGI).
        ProviderFetchError: On an OpenFIGI ``error`` payload or a malformed
            response shape.
    """
    if not isinstance(payload, list) or not payload:
        raise ProviderFetchError(
            f"OpenFIGI returned an unexpected response for {scheme}:{value}: {payload!r}."
        )
    job = payload[0]
    if not isinstance(job, dict):
        raise ProviderFetchError(
            f"OpenFIGI job result for {scheme}:{value} is not an object: {job!r}."
        )
    if job.get("error"):
        raise ProviderFetchError(f"OpenFIGI error for {scheme}:{value}: {job['error']}.")
    data = job.get("data")
    if job.get("warning") or not data:
        raise IdentifierNotResolvableError(f"OpenFIGI found no FIGI for {scheme}:{value}.")
    entry = data[0]
    if not isinstance(entry, dict):
        raise IdentifierNotResolvableError(
            f"OpenFIGI returned a match without a FIGI for {scheme}:{value}."
        )
    return entry


def _require_figi(entry: Mapping[str, Any], scheme: str, value: str) -> str:
    """Read the required ``figi`` key off a mapping entry.

    Raises:
        IdentifierNotResolvableError: If the entry carries no FIGI — a match
            without the one field that *is* the resolution.
    """
    figi = entry.get("figi")
    if not figi:
        raise IdentifierNotResolvableError(
            f"OpenFIGI returned a match without a FIGI for {scheme}:{value}."
        )
    return figi


def _first_figi(payload: Any, scheme: str, value: str) -> str:
    """Extract the first FIGI from an OpenFIGI mapping response.

    The figi-key read on top of :func:`_first_entry`; see both for the raising
    behaviour.
    """
    return _require_figi(_first_entry(payload, scheme, value), scheme, value)


def _optional_str(entry: Mapping[str, Any], key: str) -> str | None:
    """Read an optional string field off a mapping entry, defensively.

    A missing key, a ``null``, or any non-string payload value reads as
    ``None`` — never coerced, never fabricated (ADR-0090 determinism).
    """
    field = entry.get(key)
    return field if isinstance(field, str) else None
