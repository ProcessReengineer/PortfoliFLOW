# ADR-0070: Shirley Analysis Read Tools — Phase 2 (Deterministic Surfaces)

- **Status:** Proposed (stub — to be expanded before implementation)
- **Date:** 2026-06-01
- **Deciders:** PortfoliFLOW project owner
- **Tags:** ai-service, tools, shirley, read-internal, portfolio-review, benchmarks, saa, overview, frontier, analytics

> **Stub.** This records the decision direction and the questions that must
> be answered before implementation. It is intentionally lighter than a full
> ADR; the open questions below are the work of expanding it. Tracked as
> roadmap item **B6**.

---

## Context

ADR-0069 added three `READ_INTERNAL` tools (`get_limit_coverage`,
`get_saa_hypothetical_comparison`, `get_portfolio_statistics`), establishing
the pattern: a synchronous tool over `_tool_session` + `run_async_in_fresh_loop`
that calls an existing `services/` wrapper and returns a prose summary,
optionally stashing a chart-ready envelope by handle (ADR-0048).

The goal now (B6): Shirley should see **every analysis result a human sees on
the web surface that is deterministically reproducible from Postgres**. Such
results need no user-initiated run — the tool recomputes them on demand from
persistent data, so parity with the human view is automatic and there is no
staleness. This is distinct from run-bound, non-reproducible results
(scraper, kept optimizer scenarios), which require persistence first and are
covered by ADR-0071 / roadmap B7.

The deterministic surfaces not yet reachable by Shirley, each backed by an
existing, tested service:

- Front-office overview KPIs — `FrontOfficeOverviewService.get_overview_kpis`.
- Portfolio review (the investor-communication pack: cashflows, invested
  capital / NAV, multiples, IRR, vintage, region/sector treemaps, and the
  **portfolio-level total-return index** — the previously-hidden
  "Indexentwicklung") — `PortfolioReviewService.get_portfolio_overview` /
  `get_single_investment_review`.
- Benchmark comparison Stage a (per-investment vs. benchmark) and Stage b
  (asset-class composites) — `BenchmarkComparisonService.get_investment_comparisons`
  and the composite path. (Stage c, the SAA-hypothetical, is ADR-0069.)
- SAA configuration inputs (expected returns, vols, correlations, constraints)
  — `SAAService.get_configuration_full`.
- Efficient frontier — **read half only**: viewing the currently-configured
  frontier as the human sees it on the Portfolio Analysis surface —
  `PortfolioAnalysisService.compute_frontier`. Re-optimising with changed
  inputs is a write/explore capability, out of scope (later / optimizer work).

## Decision (direction)

Add a second wave of `READ_INTERNAL` tools following the ADR-0069 pattern
verbatim, one thin adapter per service method above, in
`services/tools/analysis_tools.py` (extending the same module ADR-0069 creates,
or a sibling — see Open Questions). No new analytics, no new business logic.

Proposed tool names: `get_portfolio_overview`, `get_portfolio_review`,
`get_benchmark_comparison`, `get_saa_configuration`, `get_efficient_frontier`.

## Non-Goals

- No re-optimisation / no analysis-*starting* (optimizer, SAA re-optimisation
  with new constraints). The frontier tool is read-only.
- No run-bound or external-origin results (scraper) — that is ADR-0071.
- No new analytics; adapters over existing services only.
- No projection / forecast (that is roadmap B9).

## Open Questions (resolve before implementation)

1. **`get_portfolio_review` shape.** It is the whole IC pack and will blow the
   `_DETAIL_CHAR_CAP` as a single prose block. Decide a `section` parameter
   (e.g. `cashflows` / `multiples` / `total_return_index` / `treemaps` /
   `overview`) so the model pulls one facet at a time. Mirror the
   per-investment vs. portfolio-wide split the service already exposes.
2. **Charting contracts.** Which reads warrant an ADR-0048 chart envelope
   (frontier scatter, total-return-index line) and which stay prose-only
   (overview, SAA config)? Each chart envelope needs a `__data__`
   discriminator added to `render_chart`'s allow-list (as ADR-0069 did for
   `saa_hypothetical`). Keep envelope shapes consistent — tidy
   `columns`/`rows`/`meta`, long-form for multi-series.
3. **Tool-count / mis-selection.** This wave takes Shirley from ~11 to ~16
   tools, several with overlapping-sounding names (`get_investment_data` vs.
   `get_portfolio_review` vs. `get_portfolio_overview`). Strongly couple with
   roadmap **B8** (dynamic tool-list prompt grounding); resolve whether B8
   must land first or alongside.
4. **One module vs. split.** Whether the wave extends `analysis_tools.py` or
   warrants thematic splitting once it holds 8 tools (vs. the ADR-0016
   three-line module-scope discipline).
5. **Sequencing.** Whether to land all five at once, or first the two truly
   low-friction prose-only tools (`get_portfolio_overview`,
   `get_saa_configuration`) and defer the three needing shaping
   (review/frontier/benchmark) until Q1/Q2 above are settled.

## Implementation Notes (anticipated)

- `services/tools/analysis_tools.py` — the new tools + registrations
  (`ToolClass.READ_INTERNAL`), reusing ADR-0069's helpers.
- `services/tools/chart_tools.py` — extend the `render_chart` discriminator
  allow-list per Q2.
- DI mirrors `web/routes/{overview,portfolio_review,benchmarks_attribution,
  saa_section,portfolio_analysis}.py`.
- Tests under `tests/assistants/` following `test_analysis_tools.py`.

## References

- ADR-0069 (the pattern and the first tool family), ADR-0048 (chart envelope
  by handle), ADR-0022 (`READ_INTERNAL`), ADR-0047 (tool-execution context),
  ADR-0013 / ADR-0018 (layering), ADR-0061 (benchmarks & attribution),
  ADR-0066 (cashflow-adjusted frontier returns), ADR-0067 (overview KPI strip).
- Roadmap: B6 (this item), B7 (persistent results — the complementary class),
  B8 (prompt grounding — the load-bearing companion), B9 (projection).

---

## Revision History

| Date       | Author                     | Change                              |
|------------|----------------------------|-------------------------------------|
| 2026-06-01 | PortfoliFLOW project owner | Initial stub, status Proposed       |
