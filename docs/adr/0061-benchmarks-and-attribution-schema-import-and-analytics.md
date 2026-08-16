
# ADR-0061: Benchmarks & Attribution — Schema, Import, and Analytics Architecture

- **Status:** Accepted (2026-05-25 — Phase 1a delivered end-to-end:
  schema/import/extractor, pure analytics, service/UI orchestration)
- **Date:** 2026-05-24
- **Deciders:** PortfoliFLOW project owner
- **Tags:** schema, benchmarks, attribution, excel-import, back-office, analytics, phase-7

---

## Context

PortfoliFLOW supports portfolio analysis through several pure-Python analytics layers (NAV time series, IRR, multiples, region/sector breakdowns) but has so far had no first-class concept of a market benchmark. Fund-of-funds management practitioners expect a tool to answer three questions in increasing order of sophistication:

1. **Per-investment:** How does each investment perform against the benchmark of its asset class?
2. **Per-asset-class:** When all own investments in an asset class are aggregated NAV-weighted, do the selected managers beat the market benchmark in aggregate?
3. **Per-portfolio (vs. SAA):** If the portfolio had held the SAA-prescribed weights instead of the realised weights, how would performance have compared? Both against pure benchmarks (allocation effect) and against own-fund composites (allocation effect with own managers).

These three questions cover the practical day-to-day need without requiring the operator to define a Public Market Equivalent or a Brinson decomposition. PME and full Brinson attribution remain on the roadmap as Phase 2 extensions of the same data model.

The architecture must accommodate three forward-looking concerns:

- **Composite benchmarks:** an asset class may eventually map to a weighted blend of several benchmarks (e.g. 70% MSCI World + 30% MSCI EM for a "Global Equity" asset class).
- **Sub-asset-class granularity:** a "Private Equity" asset class may later be split into "PE Mid Cap Europe", "PE Large Cap USA", etc., each with its own benchmark.
- **External data sources:** Phase 1 ships benchmark observations via the Excel import path, but the schema must accept Bloomberg, MSCI, or Cambridge Associates time series later without refactoring.

The decision must also be consistent with the established Phase-6 hard-fail-on-unknown-label discipline (ADR-0046, Region Model): the operator's intent must be unambiguous at import time, with no silent auto-creation of catalogue entries.

---

## Decision

Introduce three new schema objects under migration **b011** (`db/migrations/versions/2026_MM_DD_HHMM_b011_add_benchmarks.py`):

- **`benchmarks`** — per-tenant catalogue of benchmark definitions. Each row is `(id, tenant_id, code, display_name, description, provider_hint, ...)`. Populated by the Excel import path; unknown codes in mapping rows are hard import errors. The `provider_hint` column documents the intended data source ("Synthetic / Future: MSCI World NR EUR") without coupling the schema to a specific vendor.

- **`benchmark_observations`** — daily time series of period returns per benchmark. `(id, tenant_id, benchmark_id, as_of_date, period_return)` with `UNIQUE (benchmark_id, as_of_date)`. The `period_return` is a signed decimal (e.g. `0.005` = 0.5% daily return), matching the existing `total return actual` sheet convention. `tenant_id` is denormalised for RLS performance.

- **`asset_class_benchmark_mapping`** — many-to-many between asset classes and benchmarks with weights. `(id, tenant_id, asset_class_id, benchmark_id, weight)` with `CHECK (weight >= 0 AND weight <= 1)` and `UNIQUE (asset_class_id, benchmark_id)`. In Phase 1 each asset class has exactly one mapping row with `weight = 1.0`. Composite-benchmark support (multiple rows per asset class with weights summing to ≤ 1) is schema-ready but not exercised in Phase 1.

All three tables use the standard `apply_tenant_rls()` helper for row-level security per tenant (ADR-0035). `benchmarks` and `asset_class_benchmark_mapping` have audit triggers (low-frequency catalogue tables); `benchmark_observations` does not (high-frequency time series, analogous to `investment_navs`).

The Excel import path is extended to recognise two new sheets:

- **`Benchmarks actual`** is registered as a market reference sheet under `MARKET_REFERENCE_SHEETS` in `modules/front_office/data_import.py`, alongside `interest rates` and `AUM`. It inherits the existing parser without modification.

- **`Benchmark Mapping`** is a new sheet category with its own parser `_parse_benchmark_mapping_sheet`. The sheet contains `(asset_class, benchmark_id, weight, comment)` rows.

The investment extractor (`services/data_normalization/investment_extractor.py`) gains an `extract_benchmarks_from_snapshot()` function that produces three lists of frozen DTOs (`ImportedBenchmark`, `ImportedBenchmarkObservation`, `ImportedBenchmarkMapping`) plus a list of `ImportRowError` instances for soft failures. The extractor stays DB-free; FK resolution from codes to UUIDs happens at the service layer.

`InvestmentService.transform_benchmarks_from_upload()` persists the extracted data idempotently — re-importing the same workbook updates benchmarks by code, replaces observations atomically, and refreshes mappings. Hard failures (unknown asset-class code, unknown benchmark-id in mapping, weights outside `[0, 1]`) raise `ValidationError` with operator-actionable messages.

The pure analytics layer adds `services/analytics/benchmark_comparison.py` with three functions:

- `compute_benchmark_comparison()` — given monthly investment returns, monthly benchmark returns, and monthly risk-free returns, returns a frozen `BenchmarkComparisonMetrics` dataclass with twelve fields covering the five Phase-1 metric groups (Excess Return, Alpha+Beta+R², Tracking Error + Information Ratio, Up/Down Capture, Sharpe difference).

- `compute_asset_class_composites()` — NAV-weighted Beginning-of-Period aggregation of per-investment monthly returns to per-asset-class composite series. Returns one `AssetClassCompositeSeries` per asset class. Methodology is Time-Weighted Return (TWR), GIPS-compatible. NAV gaps are forward-filled (illiquid asset classes with quarterly NAV updates produce two zero-return months followed by a quarterly spike — an honest representation of NAV behaviour rather than artificial smoothing).

- `compute_saa_hypothetical_series()` — for a given SAA weights vector, produces two hypothetical portfolio return series (SAA × Benchmark and SAA × Composite) plus the actual NAV-weighted portfolio returns over the same period. The Brinson decomposition that follows from these three series is Phase 2.

The orchestration layer adds `services/benchmark_comparison/benchmark_comparison_service.py` analogous to `services/portfolio_review/portfolio_review_service.py`: it fetches inputs via repositories, calls the pure analytics functions, and returns ready-to-render bundles.

A new module `modules/back_office/benchmarks_attribution.py` registers under `module_area = "back_office"` and surfaces in the Back Office area shell as the **"Benchmarks & Attribution"** section, positioned between SAA and Investment Limits. The section name anticipates the Phase 2 Brinson attribution that will share the same data foundation.

Three chart specifications under `services/chart_specs/` render the three blocks (per-investment table with expandable detail charts, asset-class small-multiples grid, SAA hypothetical comparison with dual selector for SAA configuration and weight set).

Risk-free returns for Sharpe/Alpha calculations are derived from the existing `interest rates` sheet. The daily annualised rate is converted to a monthly return via `r_m = (1 + ann_rate)^(days_in_month / 365) - 1` for alignment with the monthly aggregation grid.

Excess return is defined arithmetically (`r_investment - r_benchmark`), matching the standard performance-reporting convention.

The SAA weight selector in Block 3 (Stufe c) is a two-dropdown UI: first dropdown picks the SAA configuration (all configurations in the tenant), second dropdown picks the weight set within that configuration. Weight set options are:

- **Beschlossene Target-Allocation** — the operator's `target_weight` column from `SAAAssetClassInput`. Sum must equal 100% (enforced at the SAA service; defensive validation in benchmark service raises a clear error if not).
- **Tangency-Portfolio (max Sharpe)** — from the latest `SAAOptimizationResultDTO`.
- **Minimum-Varianz-Portfolio** — from the latest `SAAOptimizationResultDTO`.

Frontier discrete points and cloud samples are intentionally excluded — they have no practical interpretation as "the portfolio I would actually have held". Weight sets that don't exist for a configuration (e.g. no optimisation run yet) are greyed out in the dropdown with a hint.

---

## Rationale

**Why three tables, not one with composite columns?**
A wide table (one row per `asset_class_id` × `month` with `benchmark_id_1`, `weight_1`, `benchmark_id_2`, `weight_2`, …) caps the composite count at schema time and produces sparse rows. The three-table normalised form is composite-fan-out-friendly, supports forward-historisation of mappings if needed in Phase 2, and matches the established many-to-many patterns elsewhere in the schema (region memberships, sector weights).

**Why mapping by `asset_class_id`, not by `investment_type`?**
`Investment.investment_type` is a hardcoded enum of seven values (`private_equity`, `private_debt`, `real_estate`, `infra_equity`, `listed_equity`, `listed_bonds`, `other`). The benchmark granularity that practitioners actually want is finer — `Government Bonds DM`, `IG Credit`, and `High Yield` all map to `investment_type = listed_bonds` but warrant separate benchmarks. The tenant-scoped `asset_classes` catalogue is the right granularity, and is forward-compatible with sub-asset-class refinements (e.g. "PE Mid Cap Europe" as its own `asset_classes` row).

**Why arithmetic excess return?**
For monthly observations and decade-scale horizons the geometric definition `(1 + r_i) / (1 + r_b) - 1` diverges from the arithmetic definition only at the third decimal place. Arithmetic is the convention in most professional reporting tools and is more intuitive (a 2pp excess return is "exactly 2 percentage points more"). Geometric becomes important for very long horizons or highly volatile pairs, neither of which is the dominant Phase-1 use case.

**Why Forward-Fill on NAVs, not interpolation?**
Illiquid asset classes (PE, Infrastructure, Real Estate) report NAVs quarterly. Linear interpolation between quarterly points would falsify volatility — the resulting return series would have artificially smooth, mathematically-implied monthly returns that no real investor experienced. Forward-fill produces an honest series: two months of zero return followed by a quarterly spike. The downstream metrics (Beta, TE, R²) are computed against this honest series. The interpretation is that for illiquid asset classes the metrics are "naive time-series comparisons" rather than risk-adjusted alphas, and the UI labels them accordingly.

**Why no caching in Phase 1?**
The expected workload is 20 investments × 120 months × 9 asset classes. Pre-aligned monthly resampling and the pandas/numpy operations involved complete in well under a second. Caching would add complexity (invalidation on NAV updates, on benchmark observations, on SAA changes) for no measurable performance benefit at the current data scale. When institutional-scale workloads (200+ investments, 240+ months) materialise, a materialised-view-backed cache is the natural extension and can be added without changing the analytics surface.

**Why a dedicated section, not an extension of Portfolio Tracking?**
Three reasons: (1) the conceptual subject is the comparison-and-attribution workflow, not the master-record workflow that Portfolio Tracking owns; (2) the three blocks of the section (per-investment, per-asset-class, per-SAA) form a coherent narrative arc that would be diluted as a tab inside another section; (3) the Phase 2 Brinson attribution extension shares the same data foundation and lives naturally in the same section, which is why the section name already includes "Attribution".

**Why anchor the SAA weight selector on `target_weight`, Tangency, and MinVar only?**
The selector's purpose is "which SAA-prescribed weight vector would I have followed if I had been disciplined?". Tangency and MinVar are the two named, operator-meaningful portfolios. The `target_weight` column is the operator's deliberate choice — the SAA they actually decided to implement, possibly after considering and rejecting Tangency. Frontier discrete points and cloud samples are intermediate calculation artefacts, not portfolio decisions.

---

## Alternatives Considered

- **Composite columns on `asset_classes`:** Add `benchmark_code`, `benchmark_weight`, `benchmark_code_2`, `benchmark_weight_2`, ... to the existing `asset_classes` table. Rejected — caps composite count at schema time, requires schema migration to extend, and breaks the established many-to-many pattern used elsewhere in the project.

- **Per-investment benchmark override:** Allow an `Investment.benchmark_id` column that overrides the asset-class-derived benchmark for specific investments. Rejected for Phase 1 — adds modelling complexity without a concrete operator workflow that demands it. The asset-class-derived benchmark covers all Phase-1 use cases. Can be added later as a NULL-able column without disturbing the existing schema.

- **PME (Public Market Equivalent) as the primary metric:** Use the Kaplan-Schoar PME or similar cashflow-based methodology instead of time-series-return-based comparison. Rejected for Phase 1 — PME requires per-investment cashflow alignment with benchmark price points and is conceptually further from the practitioner's "did my manager beat the market?" framing. PME belongs in Phase 2 as a complementary view alongside the TWR-based comparison.

- **Frequency switch (monthly vs. quarterly) in Phase 1:** Allow the operator to choose the analysis frequency. Rejected for Phase 1 — adds UI complexity without a concrete decision support need. Monthly is the right default for liquid asset classes and the "honest forward-fill" methodology makes monthly work acceptably for illiquid ones. Frequency switching is a Phase 2 polish item if it surfaces as a real need.

- **Brinson decomposition (Selection / Allocation / Interaction) in Phase 1:** Build the full Brinson attribution table on top of Stufe b) and c). Rejected for Phase 1 — Brinson attribution requires careful handling of period-level interaction effects, multiple weight-set choices, and arithmetic-vs-geometric attribution flavours. Building it on top of a settled and tested Stufe b/c data foundation is cleaner than building everything at once.

---

## Consequences

### Positive

- Benchmark comparison becomes a first-class analytics capability backed by a normalised schema that scales to Phase 2 extensions without refactoring.
- The composite-benchmark and sub-asset-class extensions are pure data operations (add mapping rows, add asset-class rows) requiring no code or schema changes.
- The Excel import path is extended through the same pattern as existing market-reference sheets — no architectural deviation.
- The "Benchmarks & Attribution" section name reserves conceptual room for the Phase 2 Brinson attribution that will share the same data foundation and UI surface.
- The pure analytics layer remains pure; the regression test `tests/regression/test_analytics_layer_pure.py` automatically validates this.

### Negative

- The hard-fail-on-unknown-label discipline (consistent with ADR-0046) means an Excel mapping row referencing a non-existent asset-class code blocks the entire benchmark import until the operator either adds the asset class or fixes the mapping. The error message must be actionable.
- For illiquid asset classes with quarterly NAV updates, the monthly metrics include two near-zero-return months for every three. The UI must label these as "naive time-series comparison" lest practitioners misread them as conventional alpha calculations.
- Phase 1 ships without caching, which becomes a constraint once tenant data grows beyond ~200 investments. The materialised-view extension is documented as a Phase 2 follow-up.

### Neutral / Follow-ups

- Phase 2 will extend the same data foundation with: Brinson attribution (Selection / Allocation), PME alongside TWR-based comparison, composite-benchmark UI for managing multiple benchmarks per asset class, sub-asset-class extension via additional `asset_classes` rows with finer granularity, and external benchmark data sources (MSCI, Bloomberg, Cambridge Associates) replacing the Excel-import-only Phase-1 path.
- The UI polish round is scheduled as a separate work package after the functional foundation is in place (roadmap A12 explicitly carves out a Phase 1a "grobe UI" and Phase 1b "Polish" split). The polish round is anticipated as a meaningful competitive differentiator versus the bar-chart-only visualisations common in proprietary tools at peer firms.

---

## Implementation Notes

- Migration: `db/migrations/versions/2026_MM_DD_HHMM_b011_add_benchmarks.py`.
- Pure analytics: `services/analytics/benchmark_comparison.py`.
- Orchestrator: `services/benchmark_comparison/benchmark_comparison_service.py`.
- Module: `modules/back_office/benchmarks_attribution.py`.
- Route: `web/routes/benchmarks_attribution.py`.
- Templates: `web/templates/_partials/benchmarks_attribution_*.html`.
- Test fixtures: `PortfoliFLOW_Testdaten_v23.xlsx` (introduces the two new sheets) is the reference workbook.
- Implementation is split across three atomic Claude Code prompts (schema/import/extractor, analytics, service/UI) per the project's prompt-atomicity convention (ADR-0014, ADR-0015).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Functional Suitability (operator can answer the three benchmark questions), Compatibility (the schema admits external data sources later without disturbing existing data), Maintainability (composite-benchmark and sub-asset-class extensions are data operations, not code changes), Reliability (hard-fail-on-unknown-label discipline prevents silent data corruption).
- **Audit evidence:** Migration b011 source, fixture workbook v23, ADR-0061, unit tests covering known-input/known-output cases for all metric computations, integration tests covering the Excel-import path end-to-end.
- **Regulatory tie-in:** Benchmark-comparison reporting is a standard component of Versorgungswerk and KVG due-diligence workflows. The arithmetic-excess-return convention and TWR composite methodology are GIPS-compatible defaults.

## References

- ADR-0009 (Excel V2 import format — sheet category model)
- ADR-0013 (Analytics layer pure and stateless)
- ADR-0034 (Persistence backend Postgres)
- ADR-0035 (Multi-tenant architecture, RLS)
- ADR-0042 (Phase 3 SAA scope and asset-class catalogue)
- ADR-0043 (Investment-domain schema)
- ADR-0045 (Charts/Statistics web migration and analytics service foundation)
- ADR-0046 (Region model — hard-fail-on-unknown-label precedent)
- Roadmap A12 (Benchmarks & Attribution — Phase 1, this ADR)

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-05-24 | Project owner + assistant | Initial decision. Three new tables under migration b011; Excel-import path extension; analytics + service layout; section under `/back-office#benchmarks-attribution`. |
| 2026-05-25 | Project owner + assistant | Accepted. Phase 1a delivered end-to-end across the three prompt-atomic implementation steps. Risk-free rate sourced from the active SAA configuration's `risk_free_rate` column (annualised scalar) because the "interest rates" Excel sheet is not persisted in the current schema; promoting that sheet to a DB-backed time series is a separate Roadmap follow-up. |
