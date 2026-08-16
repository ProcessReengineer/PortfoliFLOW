# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The :class:`MarketDataProvider` port — the async seam every adapter satisfies.

The port is the **call contract** (ADR-0091 §"The port is async"): how the
system invokes *any* provider. It is deliberately async because async is the
rule across this platform (FastAPI, SQLAlchemy 2 async, the Irene tick job);
HTTP providers (Yahoo, OpenFIGI) implement it natively over
``httpx.AsyncClient``, and a future synchronous provider (Bloomberg ``blpapi``)
bridges *locally* inside its own adapter via ``asyncio.to_thread`` — the port
never changes shape for it.

The Protocol is :func:`~typing.runtime_checkable` so callers and tests can
assert ``isinstance(adapter, MarketDataProvider)``, mirroring
``services.voice.provider.VoiceProvider``.

The port-level error hierarchy lives here too. Adapters map every
provider-raw failure (an httpx error, a vendor error payload) onto one of
these before it crosses the boundary, so downstream code catches a small,
stable set of exceptions and never a provider SDK's own exception type. The
errors are plain :class:`Exception` subclasses, defined locally — like
``services.voice.errors`` — so this package needs nothing from ``core/``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from services.market_data.dto import (
    DateWindow,
    NormalizedIdentifier,
    NormalizedSeries,
    SeriesKind,
)


class MarketDataError(Exception):
    """Base class for every error crossing the market-data port boundary."""


class UnsupportedCapabilityError(MarketDataError):
    """No adapter serves the requested ``(scheme, kind)`` combination.

    Raised by the factory when the capability matrix routes nothing, and by an
    adapter defending its own boundary when handed a ``scheme`` or ``kind`` it
    does not implement. This is the *declared* non-availability of ADR-0091
    property 2 — distinct from a fetch that returned no data.
    """


class IdentifierNotResolvableError(MarketDataError):
    """The provider could not resolve the identifier to any instrument.

    An unknown ticker at Yahoo, or an ISIN OpenFIGI maps to nothing. A
    recorded gap, never a fabricated mapping (ADR-0090 §"FIGI is the internal
    provider join-key").
    """


class ProviderFetchError(MarketDataError):
    """The provider call failed — transport error, timeout, or non-2xx status.

    Adapters map ``httpx`` transport/timeout errors and unexpected HTTP
    statuses onto this so the raw ``httpx`` exception never leaks past the
    port.
    """


class MarketDataConfigurationError(MarketDataError):
    """Required configuration for an adapter is missing or invalid.

    For example, the synthetic adapter's fixture path is unset. Secrets and
    wiring come from the environment / capability matrix, never the DB
    (ADR-0091 §"Factory & config layering").
    """


@runtime_checkable
class MarketDataProvider(Protocol):
    """Fetch a normalised series for one identifier, kind, and window.

    Implementations return a provider-blind :class:`NormalizedSeries`, never
    provider-raw JSON: every idiosyncrasy (field names, scale, calendar,
    adjusted-vs-unadjusted) is paid for inside the adapter (ADR-0091 property
    3). They raise a :class:`MarketDataError` subclass on failure — never
    return an empty or ``None`` result to signal one.
    """

    async def fetch_series(
        self,
        ident: NormalizedIdentifier,
        kind: SeriesKind,
        window: DateWindow,
    ) -> NormalizedSeries:
        """Fetch the ``kind`` series for ``ident`` over ``window``.

        Args:
            ident: The identifier to fetch against (scheme + value, ADR-0090).
            kind: The :class:`SeriesKind` to fetch.
            window: The inclusive ``[start, end]`` date window.

        Returns:
            A :class:`NormalizedSeries` whose points are already in canonical
            form. The series may be empty (no data in the window) — that is a
            real gap, distinct from an unsupported kind.

        Raises:
            UnsupportedCapabilityError: If this adapter does not serve
                ``ident.scheme`` or ``kind``.
            IdentifierNotResolvableError: If the provider cannot resolve
                ``ident`` to an instrument.
            ProviderFetchError: On transport failure, timeout, or non-2xx
                status.
        """
        ...
