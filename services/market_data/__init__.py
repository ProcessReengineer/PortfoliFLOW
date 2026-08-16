# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider-agnostic market-data ingest layer (ADR-0091, roadmap #036).

The **second** producer feeding the ``InvestmentService`` write path — a live,
provider-agnostic source of prices/NAVs, cashflows, and composition weights
that runs *alongside* the Excel import (ADR-0090's inverse: where the
``InvestmentExtractor`` turns raw Excel into a canonical intermediate, this
layer turns provider responses into the *same* canonical shape).

Provider-agnosticism is made explicit on three seams:

- the async **port** :class:`~services.market_data.provider.MarketDataProvider`
  (the call contract);
- the provider-blind **DTO**
  :class:`~services.market_data.dto.NormalizedSeries` (the result contract),
  defined around the canonical target tables, never around any provider;
- the declarative **capability matrix**
  (``config/market_data_capabilities.yaml``) that routes ``(scheme, kind)`` to
  an adapter without code change.

Adapters (Yahoo native-async, synthetic fixture-driven) pay the
one-per-provider normalisation cost internally; a future Bloomberg adapter
bridges its synchronous ``blpapi`` locally via ``asyncio.to_thread``, never
changing the port. OpenFIGI ISIN/ticker→FIGI resolution
(:func:`~services.market_data.normalisation.resolve_figi`) is a separate,
deterministic mapping seam.

This package is parallel to ``services/web_research/`` and ``services/voice/``
— never under ``services/analytics/``. It imports no SQLAlchemy, FastAPI, Qt,
repository, or LLM client (guarded by
``tests/regression/test_market_data_layer_pure.py``).
"""

from __future__ import annotations

from services.market_data.dto import (
    IDENTIFIER_SCHEMES,
    DateWindow,
    NormalizedIdentifier,
    NormalizedQuote,
    NormalizedSeries,
    SeriesKind,
    SeriesPoint,
)
from services.market_data.factory import (
    CapabilityMatrix,
    ProviderCapability,
    build_adapter,
    get_capability_matrix,
    get_provider,
    load_capability_matrix,
    resolve_provider_name,
)
from services.market_data.normalisation import resolve_figi
from services.market_data.provider import (
    IdentifierNotResolvableError,
    MarketDataConfigurationError,
    MarketDataError,
    MarketDataProvider,
    ProviderFetchError,
    UnsupportedCapabilityError,
)

__all__ = [  # noqa: RUF022 — grouped by seam; a flat sort orphans the group comments
    # DTO (result contract)
    "IDENTIFIER_SCHEMES",
    "DateWindow",
    "NormalizedIdentifier",
    "NormalizedQuote",
    "NormalizedSeries",
    "SeriesKind",
    "SeriesPoint",
    # Port (call contract) + errors
    "MarketDataProvider",
    "MarketDataError",
    "UnsupportedCapabilityError",
    "IdentifierNotResolvableError",
    "ProviderFetchError",
    "MarketDataConfigurationError",
    # Factory / capability matrix
    "CapabilityMatrix",
    "ProviderCapability",
    "load_capability_matrix",
    "get_capability_matrix",
    "resolve_provider_name",
    "build_adapter",
    "get_provider",
    # OpenFIGI normalisation
    "resolve_figi",
]
