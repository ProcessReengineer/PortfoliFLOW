# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Concrete market-data adapters (ADR-0091 §"Adapters bridge locally").

Each adapter satisfies :class:`services.market_data.provider.MarketDataProvider`
and pays the one-per-provider normalisation cost (field mapping, scale,
currency, calendar, adjusted-vs-unadjusted) internally, so everything
downstream stays provider-blind.

Present adapters:

- :mod:`services.market_data.adapters.yahoo` — native ``async`` over
  ``httpx.AsyncClient``. No bridging.
- :mod:`services.market_data.adapters.synthetic` — fixture-driven,
  fully deterministic; the test-event injection seam.

**The Bloomberg bridge seam (not yet built).** A future ``bloomberg.py`` will
wrap the synchronous ``blpapi`` C++ extension. Async is the rule on this
platform, so the bridge belongs *locally inside that one adapter*, never in
the port: its ``fetch_series`` stays ``async def`` and offloads the blocking
work with ``return await asyncio.to_thread(self._blocking_fetch, ...)`` —
keeping the event loop free while the worker thread waits on Bloomberg. No
speculative Bloomberg code is written here; this note marks where the
``asyncio.to_thread`` seam goes when the adapter lands.
"""

from __future__ import annotations
