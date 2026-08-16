# ADR-0069: Back-Office Analysis Tools for Shirley — Exposing Limit Coverage, SAA-Hypothetical Comparison, and Portfolio Statistics as `READ_INTERNAL` Tools

- **Status:** Accepted
- **Date:** 2026-06-01
- **Deciders:** PortfoliFLOW project owner
- **Tags:** ai-service, tools, shirley, limits, saa, benchmarks, statistics, analytics, read-internal, phase-6, phase-7

---

## Context

Shirley's AI-callable tool surface is, today, **investment-domain only**.
The `_register_default_tools` bootstrap in
`services/ai_service_core.py` imports exactly four tool modules —
`datastore_tools`, `chart_tools`, `web_research_tool`,
`investment_tools` — yielding these registered tools:

- **Investment domain (Postgres, working):** `list_investments`,
  `get_investment_detail`, `get_investment_nav_history`,
  `get_investment_data` (bundles `catalogue`, `nav_series`,
  `cashflow_series`, `return_metrics`, `portfolio_nav_series`).
- **In-memory DataStore:** `list_datasets`, `get_dataset_summary`,
  `get_dataset_slice`, `list_analysis_results`.
- **Other:** `generate_chart`, `render_chart`, `web_research`.

Two structural facts follow from this inventory:

1. **The four DataStore tools are inert on the web surface.** Per
   ADR-0041 the web variant never populates the in-memory `DataStore`
   singleton (the regression test
   `tests/regression/test_web_does_not_import_persistent_data_store.py`
   enforces it). On Shirley's web surface these four tools always
   return an empty store — most notably `list_analysis_results`, the
   tool a model would reach for to find a correlation matrix, an SAA
   comparison, or a statistics table.

2. **The entire back-office analytics domain is unexposed.** The
   `services/` and `analytics/` layers already compute SAA-hypothetical
   comparisons, limit coverage, correlation matrices, and per-investment
   risk metrics — but **no tool is registered for any of them**. There
   is no channel from those services to the model. Shirley cannot reach
   this data, not because of a bug in tool dispatch or a gap in the
   database, but because the registered tool family does not span it.

This was diagnosed against three representative chats the owner expects
Shirley to handle and which currently fail:

- **B — "The board thinks we'd have done just as well passively holding
  the SAA weights. Settle it."** Needs the SAA-hypothetical return vs.
  the actual book, with the Brinson allocation effect.
  `services/analytics/benchmark_comparison.py` already computes this; the
  service wrapper
  `BenchmarkComparisonService.get_saa_hypothetical(...)` returns an
  `SAAHypotheticalBundle` carrying `SAAHypotheticalEffects`
  (`allocation_effect_pp`, `selection_effect_pp`, the cumulative
  endpoints, and the actual). The chart spec
  `benchmark_saa_hypothetical` already exists. **Gap: no tool.**

- **C — "Be blunt — is Investment H earning its fee, or am I paying
  active fees for beta?"** Needs a correlation matrix *and* per-name
  Sharpe ratios. `StatisticsService.get_universe_statistics(...)`
  returns a `UniverseStatisticsBundle` carrying both the
  `correlation_matrix` and per-investment risk metrics including
  `sharpe_ratio` — i.e. C is a single service call. **Gap: no tool.**

- **A — "We've got a €40m infra-equity call landing in Q3. Against our
  end-2030 limits, does that tip anything into breach — and if so, what
  would you trim?"** Has two layers. The *present-state* headroom /
  breach picture per asset class is already computed by
  `LimitsCoverageService.get_coverage(...)`, which returns a
  `LimitsCoverageBundle` with the SAA and AnlV family coverage rows
  (`coverage_pct`, `headroom_eur`, OK/WARN/BREACH status), the breach
  KPI strip, and the limit step-lines (the caps). The *forward* layer —
  projecting exposures to end-2030 and overlaying a hypothetical Q3 call
  — has **no engine**: there is no forecast / projection capability in
  the codebase (cash-flow projection is a planned Phase-5+ module; "plan"
  sheets are explicitly unfilled future placeholders). **Gap: no limit
  tool, and no forward-projection capability.**

A secondary, compounding factor: `docs/Soul_Shirley.md` advertises
"Strategic Asset Allocation", "allocation reviews", and "risk
considerations" as capabilities, and carries an open TODO that the
system prompt should expose a *dynamic* list of currently-available
tools plus loaded-dataset context — neither of which is implemented.
Shirley is told she can do allocation and risk work, has no tool for it,
and the prompt does not ground her in her real inventory. That mismatch
is what turns the missing-tool gap into the observed failure (silent
inability or confabulation). The prompt-grounding work is **related but
out of scope here** (see Follow-ups).

This decision is **audit-relevant**: the new tools read SAA-,
limit-, and benchmark-classified portfolio data and inherit the
tenant-isolation posture of ADR-0035 / ADR-0047.

## Decision

PortfoliFLOW adds a **back-office analysis tool family** — three new
`READ_INTERNAL` tools — in a new module
`services/tools/analysis_tools.py`, registered at import time and added
as the fifth default tool module in `_register_default_tools`. The
tools wrap **existing service-layer entry points**; this ADR introduces
**no new business logic** and **no new analytics**.

| Tool | Wraps | Serves |
|------|-------|--------|
| `get_limit_coverage` | `LimitsCoverageService.get_coverage(...)` | Chat A (present-state) |
| `get_saa_hypothetical_comparison` | `BenchmarkComparisonService.get_saa_hypothetical(...)` | Chat B |
| `get_portfolio_statistics` | `StatisticsService.get_universe_statistics(...)` | Chat C |

Each tool follows the established Postgres-native tool pattern exactly
(ADR-0047): a synchronous `Callable[..., str]` that builds its workflow
as a coroutine, runs it through
`services.tools._async_bridge.run_async_in_fresh_loop`, opens a
short-lived loop-local session via the `_tool_session` context manager,
constructs the service from repositories — mirroring the web routes'
`_build_service(db_session)` DI — and reads under `tenant_context()`.
When the tool-execution context is unset (the GUI path), each tool
returns the same clear explanatory string `investment_tools.py` uses,
not an exception.

### Tool contracts

**`get_limit_coverage`** — present-state and historical limit coverage.
Returns a prose summary of the most-recent-Stichtag picture: per asset
class, the SAA and AnlV coverage percentage, `headroom_eur`, and
OK/WARN/BREACH status, plus the breach-count KPI strip. It accepts only
the parameters `get_coverage` already supports (`from_date`, `to_date`,
`cut_over`), passed through unchanged. It performs **no projection and
no what-if overlay** (see Non-Goals).

**`get_saa_hypothetical_comparison`** — the SAA-vs-actual question.
Returns a prose summary carrying the cumulative endpoints (SAA ×
benchmark, SAA × composite, actual) and the `allocation_effect_pp` /
`selection_effect_pp`. Because the bundle requires an SAA configuration
/ weight-set selection, the tool accepts an optional selection argument
and, when omitted, resolves the tenant's default per the service's own
default-selection behaviour, naming the selected configuration in its
summary. This tool **additionally** stashes a chart-ready tidy envelope
by handle (see "Charting scope").

**`get_portfolio_statistics`** — the risk/correlation question.
Returns a prose summary of the per-investment KPI cards (annualised
return, volatility, Sharpe) and the pairwise correlation among the
universe (or a named subset). It accepts the parameters
`get_universe_statistics` already supports (`investment_ids`,
`as_of_date`, `risk_free_rate`, `active_only`).

### Charting scope

All three tools are **prose-first** read tools; their summary strings
are sufficient to answer B and C as written, and to answer the
present-state of A. For the **one chart** B explicitly asks for
("here's the chart for the board"), `get_saa_hypothetical_comparison`
also stores a chart-ready tidy `columns`/`rows`/`meta` envelope
server-side via `store_tool_data` and returns its opaque data handle in
the summary, exactly per the by-handle contract of ADR-0048;
`render_chart`'s discriminator allow-list gains the new
`saa_hypothetical` bundle so it can resolve and render it. The existing
`benchmark_saa_hypothetical` chart spec is the rendering target.

Charting for limit coverage (small-multiples) and statistics
(correlation heatmap, sparklines) is **explicitly out of scope** for
this ADR. Those have dedicated chart specs reachable from the
Back-Office and Charts & Statistics web surfaces; wiring them into
Shirley's `render_chart` path is a separate, deferred decision. This
keeps the present change to: prose answers for all three chats, plus
the single chart B needs.

### Non-Goals (explicit)

- **No cash-flow / exposure projection. No forecast engine. No
  what-if overlay.** The forward half of Chat A — projecting exposures
  to end-2030 and adding a hypothetical Q3 call — is **deliberately not
  built here**. It is a substantial capability that must land as its own
  work item with its own ADR before first release, and folding a partial
  version into a limit-read tool now would create exactly the
  half-working, later-to-be-untangled structural debt this decision
  refuses to take on. `get_limit_coverage` answers "where is headroom
  today / what is in breach now"; it does not answer "what happens at
  end-2030 if we add €40m." Shirley must be able to say so plainly
  rather than improvise a projection. The projection capability is a
  **roadmap item** (`docs/roadmap.md`), pre-release-required, not
  implemented in this change.
- **No new analytics or business logic.** The tools are thin adapters
  over existing, tested services.
- **No change to the `ToolRegistry`, `stream_response`, or Qt-adapter
  signatures.** Same minimal blast radius as ADR-0047.
- **No DataStore-tool removal and no prompt-grounding work** in this
  ADR (see Follow-ups).

## Rationale

- **The capability already exists; only the seam is missing.** Every
  number in the three target answers is computed today by a tested
  service. Exposing them as tools is the smallest change that closes the
  gap, and it adds no logic to audit beyond the adapter.
- **Pattern reuse over invention.** The tools reuse ADR-0047's context
  propagation, `_async_bridge`, and `_tool_session` verbatim, and
  ADR-0022's `READ_INTERNAL` class. A reviewer who has read
  `investment_tools.py` has read these.
- **Service layer, not analytics layer.** The tools call the
  `services/` wrappers (`LimitsCoverageService`,
  `BenchmarkComparisonService`, `StatisticsService`), not the pure
  `analytics/` functions directly, preserving the layering of ADR-0013
  / ADR-0018 and matching how the web routes consume them.
- **Prose-first keeps the token and trust budget honest.** Returning
  compact summaries (with a by-handle envelope only where a chart is
  actually requested) follows ADR-0048's discipline: the model decides
  what to say and what to chart; bulk data never transits the model's
  context.
- **Refusing the forecast is the load-bearing decision.** Scoping A to
  present-state is what prevents this convenience change from quietly
  pulling in a projection model. The boundary is stated so it cannot be
  eroded "while we're in there."

## Alternatives Considered

- **Fold a forward-projection / what-if overlay into
  `get_limit_coverage` to fully answer A.** Rejected: it introduces an
  un-specified projection model as a side effect of a read tool — the
  precise structural debt this decision exists to avoid. Projection is
  its own roadmap item with its own ADR.
- **Two separate tools for C (one correlation, one statistics).**
  Rejected: `get_universe_statistics` already returns both in one
  bundle; one service call should be one tool.
- **A dedicated SAA-caps tool plus an exposure tool for A** (instead of
  the single coverage tool). Rejected per scope: the coverage bundle
  already carries the SAA caps (via the family coverage + step-lines),
  so a single `get_limit_coverage` covers A's present-state without a
  second tool. Keeping A to one tool was an explicit constraint.
- **Hydrate analyses into the in-memory `DataStore` so the existing
  `list_analysis_results` "just works."** Rejected for the same
  reasons ADR-0047 rejected the hydration bridge: it violates ADR-0041
  and re-creates the tenant-blind global singleton.
- **One tool module per service** (three new files). Rejected for
  now: one `analysis_tools.py` holding three thin adapters keeps the
  bootstrap to one import line and mirrors `investment_tools.py` (four
  tools, one module). Split later only if a module grows unwieldy.
- **Do nothing.** Rejected: Shirley's advertised allocation/risk
  capabilities remain unreachable, and the failure mode (silent
  inability or confabulation against an empty DataStore) persists.

## Consequences

### Positive

- Shirley's web surface gains working, RLS-scoped, read-only access to
  limit coverage, the SAA-hypothetical comparison, and portfolio
  statistics. Chats B and C work end-to-end; Chat A works for its
  present-state half, with an honest boundary on the forward half.
- The SAA-hypothetical chart B asks for renders via the existing chart
  spec, through the existing by-handle render path.
- No new analytics to validate; the adapters inherit the existing
  service test coverage.

### Negative

- Three more tools in the registry widen the model's choice surface;
  clear, scope-bounded descriptions (and, eventually, the dynamic
  tool-list prompt grounding) mitigate mis-selection.
- `get_limit_coverage` can answer A only partially. Until the
  projection capability lands, Shirley must explicitly decline the
  forward projection rather than improvise — a behaviour the tool
  description and (later) the prompt must make unambiguous, lest the
  partial answer read as a complete one.
- A fresh loop-local engine per call (inherited from ADR-0047's
  amendment) — negligible at Shirley's human-paced call volume.

### Neutral / Follow-ups

- **Forward projection / cash-flow projection (roadmap,
  pre-release-required).** The forward half of Chat A. Tracked in
  `docs/roadmap.md`; to be specified in its own ADR. **Not** built here.
- **DataStore-tool hygiene on the web surface.** The four inert
  DataStore tools should be hidden or gated on the web so Shirley does
  not spend turns querying an empty store. Related to this work but
  deferred to a separate change.
- **Shirley prompt grounding** (the open `Soul_Shirley.md` TODO): a
  dynamic, accurate tool list and loaded-dataset context in the system
  prompt. The single highest-leverage complement to this ADR; deferred
  to its own change so this one stays a pure tool-exposure decision.
- **Statistics / limits charting in Shirley.** Heatmap, sparkline, and
  limit small-multiples rendering through `render_chart`. Deferred.

## Implementation Notes

- Affected modules / files:
  - `services/tools/analysis_tools.py` (new) — the three
    `READ_INTERNAL` tools and their `register_tool` calls; reuses
    `_tool_session`, `run_async_in_fresh_loop`, `get_tool_context`,
    and (for the SAA tool) `store_tool_data`.
  - `services/ai_service_core.py` — one import line in
    `_register_default_tools` (five default tool modules, not four).
  - `services/tools/chart_tools.py` — extend `render_chart`'s
    discriminator allow-list with the `saa_hypothetical` bundle.
- Service construction mirrors the existing web-route DI:
  - `LimitsCoverageService(investments, navs, aums, limits,
    asset_classes)` — cf. `web/routes/limits.py::_build_service`.
  - `BenchmarkComparisonService(investments, navs, asset_classes,
    benchmarks, benchmark_observations, mappings, saa_service)` — note
    the `SAAService` dependency.
  - `StatisticsService(investments, navs)`.
- Related tests:
  - `tests/assistants/test_analysis_tools.py` (new) — context-not-set
    degradation (no DB) + DB-backed happy path for each of the three
    tools; assert `get_limit_coverage` performs no projection.
  - `tests/assistants/test_chart_tools.py` — `render_chart` accepts the
    new `saa_hypothetical` discriminator and rejects unknown ones.
  - `tests/assistants/test_tool_registry.py` — the three new tools
    register as `READ_INTERNAL` and appear in `get_tool_definitions`.
  - `tests/regression/test_ai_service_core_qt_free.py` — guards that
    `analysis_tools.py` pulls no PyQt6 into the `services/` import graph.
- Layering: `analysis_tools.py` imports only from `core/`,
  `services/`, and `services/tools/` helpers; it must not import from
  `web/` and must not import PyQt6 (ADR-0038). It calls the `services/`
  wrappers, never the `analytics/` functions directly (ADR-0013 /
  ADR-0018).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Functional Suitability
  (the back-office analytics domain becomes reachable to the assistant),
  Security (the tools inherit the ADR-0047 tenant-isolation channel and
  ADR-0035 RLS scoping), Maintainability (thin adapters over tested
  services; no duplicated analytics).
- **Regulatory references:** The limit-coverage tool surfaces
  Anlagegrenzen status (SAA / AnlV) computed by the Phase-7 engine
  (ADR-0055/0056/0057/0060); it reports, and does not alter, regulated
  limit evaluation. As a `READ_INTERNAL` tool it makes no writes and no
  external effects (ADR-0022).
- **Audit evidence:** the three `register_tool(..., tool_class=
  ToolClass.READ_INTERNAL)` calls in `analysis_tools.py`; the
  context-propagation bracket inherited from
  `web/routes/chat.py::chat_stream` (ADR-0047); the explicit no-projection
  assertion in `test_analysis_tools.py`; the `render_chart` discriminator
  allow-list and its test.

## References

- Related ADRs: ADR-0047 (tool-execution context — reused verbatim),
  ADR-0022 (tool trust classes — the new tools are `READ_INTERNAL`),
  ADR-0048 (two-axis chart architecture and the by-handle envelope —
  the SAA chart path), ADR-0041 (persistence entry-points — why the
  DataStore tools are inert on web), ADR-0035 (RLS tenant isolation),
  ADR-0013 / ADR-0018 (analytics-pure / service-repository layering),
  ADR-0061 (benchmarks & attribution analytics — the SAA-hypothetical
  source), ADR-0055 / ADR-0056 / ADR-0057 / ADR-0060 (the limit-coverage
  engine), ADR-0049 (Shirley tool orchestration).
- Deferred work: `docs/roadmap.md` (cash-flow / exposure projection —
  pre-release-required); the open prompt-grounding TODO in
  `docs/Soul_Shirley.md`.

---

## Revision History

| Date       | Author                     | Change                         |
|------------|----------------------------|--------------------------------|
| 2026-06-01 | PortfoliFLOW project owner | Initial draft, status Proposed |
| 2026-06-01 | PortfoliFLOW project owner | Implemented; status Accepted   |
