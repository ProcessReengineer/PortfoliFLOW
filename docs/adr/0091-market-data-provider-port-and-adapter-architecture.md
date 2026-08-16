# ADR-0091: Market-Data Provider Port, Normalised DTO, and Adapter Architecture — An Async-First Port with a Provider-Blind Result Contract and Sync Adapters Bridged Locally

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Live Data Import (provider-agnostic ingest)
- **Tags:** market-data, data-import, ports-and-adapters, dto-contract, concurrency, architecture

---

## Context

Live import must be **provider-agnostic**: the same downstream contract
regardless of whether data comes from Yahoo Finance, OpenFIGI, Bloomberg,
or Preqin. The first providers in scope are the free ones (Yahoo,
OpenFIGI); the architecture must let Bloomberg / Preqin be **added later
without touching downstream code**.

The providers fall into two fundamentally different concurrency worlds:

- **HTTP/REST providers** (Yahoo, OpenFIGI, Preqin REST): natively
  expressible as `async` over `httpx.AsyncClient`. They fit the
  platform's async spine bruchlos.
- **Bloomberg `blpapi`**: a synchronous C++ extension with its own
  session semantics, IP-whitelisting, and blocking event polling. There
  is **no native async**; a naive call blocks the event loop, freezing
  every tenant on that process.

The platform is **async throughout**: FastAPI, SQLAlchemy 2 async, the
Irene tick job. A blocking synchronous call inside the event loop is an
operational outage, not a style issue. The first design question is
therefore: **on which side of the sync/async divide is the port defined,
and who builds the bridge?**

### The provider set is open-ended — heterogeneity is the real risk

Which data providers customers ultimately use is **not known in
advance**. Yahoo and OpenFIGI are the free starting point; Bloomberg and
Preqin are named for later; PitchBook or others may follow. These
providers are not merely different endpoints — they have genuinely
different **world models**: Yahoo yields simple adjusted EOD prices;
Bloomberg speaks field mnemonics across every asset class; PitchBook and
Preqin are private-markets sources with fund-level NAVs and statement-day
semantics rather than daily ticks; scales, currencies, calendars, and
which metrics even *exist* differ per provider.

An adapter therefore has **two contracts**, and they must be reasoned
about separately:

1. **The call contract (upward, to the system)** — how the system invokes
   *any* adapter. This is the port signature. It is trivially uniform: a
   new provider implements the same method.
2. **The result contract (the DTO)** — *what* an adapter returns. This is
   the "single format all import adapters dock onto." Its stability is
   the **entire** protection against unknown future providers: as long as
   the DTO is stable and provider-blind, a new provider is *only* a new
   adapter and *zero* change everywhere else. If provider idiosyncrasies
   leak into the DTO, the ports-and-adapters value collapses.

The dominant failure mode is designing the DTO around the *first*
provider (Yahoo's simple world) and discovering it cannot express
Bloomberg's fields or PitchBook's fund-level cadence. This ADR fixes the
DTO around the **canonical domain** (the target tables), not around any
provider, so the result contract survives providers not yet chosen.

### What exists and is reusable

- **Ports & Adapters is the established project pattern**: `voice/`
  (`provider.py` Protocol + `factory.py` + `openai_provider.py`),
  `auth/backend.py`. A new external boundary should follow it.
- **A sync↔async bridge already exists and is blessed**:
  `services/tools/_async_bridge.py`, introduced to bridge the (sync) Qt
  tool world into the async runtime. The team knows and accepts
  `asyncio.to_thread`-style bridging.
- **External-network layers already live parallel to analytics**:
  `web_research/` and `voice/` sit under `services/`, outside the pure
  analytics layer. `tests/regression/test_analytics_layer_pure.py`
  forbids DB/FastAPI/Qt/network dependencies inside
  `services/analytics/`.
- **A canonical-intermediate precedent already exists**: the
  `InvestmentExtractor`
  (`services/data_normalization/investment_extractor.py`) turns raw Excel
  into a canonical normalised shape consumed by the `InvestmentService`
  write path. The DTO defined here is the **inverse of the same idea** for
  providers, feeding the *same* write path — which is exactly why the
  Excel format can serve as the reference contract for what a complete
  record is.

## Decision

Make provider-agnosticism explicit on **three levels**: (1) a uniform
**async port** (call contract); (2) a stable, provider-blind
**normalised DTO** (result contract) defined around the canonical domain;
(3) a declarative **capability matrix** that routes without code change.
Implement each provider as an **adapter**, and place the sync→async bridge
**locally inside the one adapter that needs it** (Bloomberg). Resolve
providers via a factory reading the capability matrix.

### Placement

A new package `services/market_data/` — parallel to `web_research/` and
`voice/`, **never** under `services/analytics/` (which must stay
network-free per ADR-0013). Suggested shape:

```
services/market_data/
  __init__.py
  provider.py        # the async Port (Protocol/ABC)
  dto.py             # NormalizedSeries / NormalizedQuote — the result contract
  factory.py         # capability-matrix-driven adapter resolution
  normalisation.py   # OpenFIGI ISIN/ticker -> FIGI (deterministic)
  adapters/
    yahoo.py         # native async (httpx)
    bloomberg.py     # sync blpapi, bridged via asyncio.to_thread
    preqin.py        # (later)
    pitchbook.py     # (later — illustrates: new adapter + matrix entry only)
```

### The port is async

```python
class MarketDataProvider(Protocol):
    async def fetch_series(
        self,
        ident: NormalizedIdentifier,
        kind: SeriesKind,          # nav/price | dividend | coupon | ...
        window: DateWindow,
    ) -> NormalizedSeries: ...
```

The port returns a **normalised DTO**, never provider-raw JSON. Every
provider idiosyncrasy (Yahoo field names, Bloomberg field mnemonics) is
paid for **inside the adapter**, exactly as the `InvestmentExtractor`
produces a canonical intermediate from raw Excel. Downstream — the ingest
write path (ADR-0092) — consumes only the DTO and is provider-blind.

### The result contract: the normalised DTO (the single format adapters dock onto)

The DTO is the durable interface every present and future adapter targets.
It is specified here because its stability — not the port signature — is
what lets an unknown provider (PitchBook, Preqin, others) be added as a
pure adapter with zero downstream change.

**Defined around the canonical domain, not any provider.** The DTO's
fields are exactly what the target tables (`investment_navs`,
`investment_cashflows`, and the historised composition weights of
ADR-0079 / ADR-0080) can absorb, plus the identity it was fetched
against — no more. The DTO is the inverse of the Excel extractor's output
and lands in the same `InvestmentService` write path. Concretely, a
`NormalizedSeries` carries: the `NormalizedIdentifier` it was fetched
against (scheme + value, ADR-0090); the provider name (provenance); the
`kind` (`nav_price` | `dividend` | `coupon` | `weight_*` | …); a currency;
and an ordered set of `(as_of_date, value)` points already in canonical
form. A `NormalizedQuote` is the single-point degenerate case.

The DTO contract has **four load-bearing properties**:

1. **Provider-blindness.** No field carries a provider-specific name,
   code, or unit. If PitchBook names a metric `PB_NAV_QTR`, the PitchBook
   adapter maps it onto the canonical NAV field and that name **dies in
   the adapter**. Test of correctness: a reviewer reading only a DTO
   instance must not be able to tell which provider produced it.

2. **Explicit non-availability, never a silent gap.** Providers cover
   different metrics: Yahoo has no YTM/duration; a private-markets source
   may have fund NAVs but no daily prices; Bloomberg has nearly
   everything. The DTO (and the capability matrix below) must distinguish
   *"this provider does not supply this metric at all"* from *"this metric
   was empty for this date."* Otherwise the ingest path cannot tell a
   respectable `NULL` from a real gap. Non-availability is represented
   explicitly (an absent `kind` from the declared coverage), not as a
   stray `None` inside a series.

3. **Units, scale, and calendar normalised at the adapter edge.**
   Currency, scale (units vs. thousands), timezone / statement-day
   convention, and adjusted-vs-unadjusted prices are pulled to the
   canonical convention **inside the adapter** before the DTO is built:
   values are EUR-capable, `as_of_date` is a statement day (matching the
   `investment_navs` DATE semantics), and cashflow timestamps follow the
   12:00-UTC convention the extractor already uses (ADR-0043 §3). The DTO
   carries only already-normalised values. This is expensive **once per
   provider** — the deliberate price of keeping everything downstream
   provider-blind.

4. **Identity and provenance carried through.** The DTO states which
   `NormalizedIdentifier` and which provider produced it, so the write
   path can set `source` and `ingest_origin = 'live'` (ADR-0092) without
   re-interrogating the adapter.

Sign conventions and the seven canonical `flow_type` / `nav_kind` values
are enforced at DTO construction, mirroring the extractor's
strict-validation boundary (ADR-0043 §3): the adapter is the boundary
that guarantees them, so downstream code can trust them.

### The capability matrix: routing without code change

Provider-agnosticism has a third seam beyond port and DTO: **which
provider can serve which identifier scheme / instrument type / metric**.
This is a **declarative fixture**, not code — the analogue of
`config/scraper_model_capabilities.json` and `config/web_research.yaml`.
Adding PitchBook is then: one adapter file **plus one entry** in this
matrix. The factory reads the matrix and routes; no existing adapter, no
DTO, no write path, and no analytics module changes. The matrix is also
the authoritative source for property (2)'s coverage declaration — it
states, per provider, which `kind`s are supported at all.

### Adapters bridge locally, not the port

- **Yahoo / OpenFIGI / Preqin-REST**: implement `async def` natively with
  `httpx.AsyncClient`. No bridging, no overhead.
- **Bloomberg**: the adapter is still `async def`, but wraps its blocking
  `blpapi` work in a worker thread:

  ```python
  async def fetch_series(self, ...):
      return await asyncio.to_thread(self._blocking_bloomberg_fetch, ...)
  ```

  The event loop stays free while the worker thread waits on Bloomberg.
  `blpapi` session lifetime and thread-affinity concerns are the
  adapter's **local** responsibility (a session opened and consumed
  inside the `to_thread` call, or a dedicated single worker); they never
  leak into the port.

### Rationale: the bridge belongs where the exception is

In this system async is the **rule** (web, DB, HTTP providers, the tick
job all run against async repositories); Bloomberg's sync nature is the
**one exception**. An async-first port bridges at exactly one site (the
Bloomberg adapter). A sync-first port would instead force a bridge at
*every* HTTP adapter **and** at every async-DB write — maximising the
number of bridges rather than minimising it. "Bloomberg-ready" does not
mean "the port is synchronous"; it means "a synchronous provider can slot
in cleanly", which `asyncio.to_thread` delivers.

### Factory & config layering

The factory (following the `voice/factory.py` precedent) resolves an
adapter from the capability matrix above. Config separates cleanly into
three layers:

- **Secrets** — API keys, endpoints, IP-whitelisting details — come from
  environment / `.env` (`.env.example` updated), **never** the DB, never a
  generated artifact.
- **Wiring & capability** — "Yahoo does listed-equity EOD, not PE";
  "PitchBook serves fund NAVs" — is the declarative capability matrix, a
  versioned fixture.
- **Per-tenant cadence** belongs to the schedule config in ADR-0093, not
  here.

## Consequences

- Adding Bloomberg / Preqin / PitchBook later is a new file under
  `adapters/` plus one capability-matrix entry — **zero** downstream
  change to the DTO, write path, or analytics.
- The market-data layer is independently testable with fake adapters
  (mirroring the `voice` / `web_research` test approach), no live
  entitlement required for CI. The DTO's provider-blindness is itself
  testable: a golden DTO fixture is asserted identical in shape whether
  produced by the Yahoo fake or the Bloomberg fake.
- A regression test asserts `services/analytics/` still imports no
  market-data / network module, preserving analytics purity.
- The normalised DTO becomes a stable internal contract; provider churn
  is absorbed at the adapter edge. Its four properties (provider-
  blindness, explicit non-availability, edge-normalised units/calendar,
  carried provenance) are the acceptance criteria for any new adapter.
- Explicit non-availability lets the ingest path (ADR-0092) treat a
  respected `NULL` and a true gap differently — a prerequisite for the
  Excel-precedence guard to reason correctly about what a live fetch
  actually delivered.
- The one-per-provider normalisation cost is borne knowingly at the
  adapter edge; it is the price of everything downstream staying
  provider-blind.

## Alternatives considered

- **Sync-first port with async bridges.** Rejected: pushes bridging onto
  every HTTP provider and every async-DB write — the common path pays for
  the rare provider. Poor fit for an async-throughout platform.
- **Per-provider bespoke services with no shared port.** Rejected:
  defeats provider-agnosticism; downstream code would branch per
  provider.
- **Running Bloomberg in-process in the web workers.** Rejected: blocking
  `blpapi` in an async worker freezes tenants; Bloomberg belongs in the
  out-of-process job (ADR-0093) regardless of port shape.
- **DTO shaped around the first provider (Yahoo).** Rejected: a
  Yahoo-shaped DTO cannot express Bloomberg field mnemonics or
  private-markets fund-level cadence; the contract would break on the
  second real provider. The DTO is fixed to the canonical target-table
  domain instead.
- **Passing provider-raw payloads downstream with a `provider` tag.**
  Rejected: pushes provider branching into the write path and analytics,
  destroying provider-agnosticism — the exact failure ports-and-adapters
  exists to prevent.
- **Silent `None` for unsupported metrics (no capability declaration).**
  Rejected: indistinguishable from a genuine data gap; the ingest guard
  could not reason about what a fetch actually returned. Coverage is
  declared explicitly in the capability matrix.
