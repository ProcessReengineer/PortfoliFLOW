# ADR-0102: Statistics, SAA, and Benchmark Currency Contract — Extending the Conversion Boundary to the Portfolio-Analysis Layer

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** PortfoliFLOW project owner
- **Tags:** analytics, fx, currency, statistics, saa, benchmark, engine-contract, phase-8
- **Supersedes:** —
- **Amends:** —
- **Successor to:** ADR-0099 §6 (which named this contract as a deliberate follow-up rather than solving it)
- **Roadmap:** #040 (Statistics / SAA currency contract)

---

## Context

ADR-0099 introduced a per-tenant **functional currency** and a **single conversion boundary** (§4): monetary series are converted from their position currency into the functional currency at the data-assembly seam *in front of* the pure analytics layer (ADR-0013), so `services/analytics/` keeps its single-currency contract untouched. That boundary was wired into exactly two consumer seams — `PortfolioReviewService` (Seam A, `float64` series) and `LimitsCoverageService` (Seam B, `Decimal` amounts) — plus the Front-Office Overview (ADR-0101).

ADR-0099 §6 **explicitly deferred** the rest:

> *"Statistics / frontier currency contract — total-return-based statistics differ between local-currency and functional-currency measurement for foreign-currency positions. The statistics/SAA/archetype layer keeps its current behaviour; defining its currency contract is named as a follow-up, not solved here."*

This ADR is that follow-up. It is a **successor decision**, not a defect fix: ADR-0099 deferred consciously because the return-measurement question is a genuine methodological choice, not a mechanical conversion.

### What still measures nominally

Three services assemble monetary series from raw `nav_value` / cashflow `amount` and hand them to the analytics layer **without conversion**. On a mixed-currency book they add e.g. USD NAV into a EUR total as if `1 USD = 1 EUR`:

1. **`PortfolioAnalysisService`** (`services/portfolio_analysis/portfolio_analysis_service.py`)
   — the efficient-frontier / current-portfolio / weights surface.
   - `nav_series = pd.Series(data=[float(n.nav_value) ...])` — raw, per investment.
   - `_latest_nav_weights(...)` sums raw last-observation NAVs across investments → **current-portfolio weights and marker are wrong** on a mixed book (a USD 500 cash position is weighted as 500, not its 450 EUR value).
   - Return series feed `derive_expected_returns_and_cov` → the covariance/frontier is computed on unconverted returns.

2. **`StatisticsService`** (`services/statistics/statistics_service.py`)
   — the Statistics section tiles; read by Shirley's `get_portfolio_statistics`.
   - Currency-*aware but non-converting*: it records `currency_by_name[inv.name] = inv.currency` per investment but never converts.
   - Per-investment cards are each in their own currency (internally consistent); the **cross-investment correlation matrix** (`compute_correlation_matrix` over mixed-currency return series) measures a quantity no functional-currency investor experiences.

3. **`BenchmarkComparisonService`** (`services/benchmark_comparison/benchmark_comparison_service.py`)
   — Benchmarks & Attribution (Stages a/b/c); read by Shirley's `get_saa_hypothetical_comparison`. Three raw-NAV assembly sites:
   - **Stage a** — `_build_investment_return_series` (per-investment total return vs. its own benchmark). *Currency-internal*: excess return is invariant when investment and benchmark share currency.
   - **Stage b** — `_build_composite_series` NAV-weights across investments *within an asset class* → cross-currency aggregation when an asset class holds foreign-currency positions.
   - **Stage c** — `_build_actual_portfolio_returns` NAV-weights across *all* investments → the "actual" line of the SAA-hypothetical comparison, cross-currency.

### Why it matters now

The two halves of the application visibly disagree on any mixed-currency book, including the shipped v31 sample (USD money-market fund, `Cash USD`, plus EUR funds). Worked example, at the sample USD rate `0.90` EUR:

| | Review / Overview (converted) | Statistics / SAA (nominal) |
|---|---|---|
| Portfolio total (630 EUR funds + 500 USD cash) | 630 + 450 = **1 080 EUR** | 630 + 500 = **1 130** (no currency) |
| USD-cash weight | 450 / 1080 = **41.7 %** | 500 / 1130 = **44.2 %** |

A demo viewer flipping from the Review KPI header (1 080 EUR) to the Portfolio-Analysis "Current Portfolio" card sees two different totals for the same date, unlabelled. This is the most demo-dangerous open backlog item. Shirley reports the nominal numbers in prose until this lands, so the inconsistency is not confined to one screen.

### Confirmed boundary of the problem

- **Already correct (out of scope):** Portfolio Review, Investment Limits, Front-Office Overview.
- **Irene:** confirmed-negative — Irene's finding engine (`irene_floor.py`, `irene_delta.py`) does not construct or read any of the three services above; it consumes limit-coverage and NAV-floor signals already on the converted / per-investment path.
- **Exports / PDF (#001):** not yet built; no current consumer of the nominal bundles.

---

## Decision

Extend the ADR-0099 §4 conversion boundary — **the same `build_portfolio_fx_converter` / `PortfolioFxConverter.convert_series` seam, no second idiom** — to the three services above. Measurement moves into the tenant's functional currency, consistent with Review/Limits/Overview.

### 1. Returns are measured in functional currency (including FX effect)

For foreign-currency positions, NAV series are converted **before** return derivation, point-in-time (each observation at the carry-forward rate of its own date). The resulting returns therefore **include the FX effect** — the correct semantics for a functional-currency investor, and the identical choice ADR-0099 §4 already made for the Review path's IRR/TVPI/DPI.

Consequently:
- The **efficient frontier** is optimised on functional-currency returns.
- **Covariance, volatility, and the correlation matrix** are computed on functional-currency returns.
- **Levels** — totals, current-portfolio weights, composition, per-investment markers, composite NAV weights — are converted and become consistent with Review/Overview by construction.

Local-currency measurement is **not** offered as a secondary view in this ADR. Decomposing a functional-currency return into its asset-performance and currency components (attribution) is a distinct, later capability — **raised as roadmap #045** (FX / asset-return attribution), out of scope here.

### 2. The seam, per service

Each service gains the two repositories the Review seam already uses — `TenantRepository` and `FxRateRepository` — in its constructor, builds one converter per request from the position currencies actually present, and converts each monetary series from `inv.currency` immediately after the raw series is assembled and **before** any aggregation or return derivation:

```python
fx = await build_portfolio_fx_converter(
    tenants=self._tenants,
    fx_rates=self._fx_rates,
    position_currencies=[inv.currency for inv in investments],
)
...
nav_series = fx.convert_series(nav_series, inv.currency)   # then aggregate as before
```

Specific application points:

- **`PortfolioAnalysisService.compute_frontier`** — convert each `nav_series` before `compute_cashflow_adjusted_return_series`; convert each cashflow `amount` series point-in-time (reuse the Review `_convert_cashflow_frame` shape); `_latest_nav_weights` then operates on converted series (no change to the method itself — it sees converted input).
- **`StatisticsService.get_universe_statistics`** — convert each `nav_series` before `compute_total_return_series`. The already-tracked `inv.currency` supplies the `from_currency`.
- **`BenchmarkComparisonService`** — convert at all three assembly sites (`_build_investment_return_series`, `_build_composite_series`, `_build_actual_portfolio_returns`). Stage a is converted for consistency even though excess return is largely currency-invariant; converting uniformly avoids a per-site currency exception and keeps one rule.

### 3. Analytics purity is preserved

No analytics function changes. Conversion stays entirely in the data-assembly services (outside `services/analytics/`). `tests/regression/test_analytics_layer_pure.py` remains green — the fix is a seam change, not an engine change.

### 4. `MissingFxRateError`, never a silent 1:1

Each service's assembly may now raise `MissingFxRateError`. The three section routes — `web/routes/statistics.py`, `web/routes/portfolio_analysis.py`, `web/routes/benchmarks_attribution.py` — catch it and render an **error partial with HTTP 200** (HTMX section swap), exactly as `web/routes/overview.py` and `web/routes/limits` already do (`overview_error.html`, `limits_error.html` precedent). A new `statistics_error.html`, and reuse/addition of partials for the analysis and benchmark sections, carry `error_type` / `error_message`.

### 5. Invisibility invariant (single-currency tenants)

`build_portfolio_fx_converter`'s zero-read fast-path guarantees that a tenant whose entire universe is in the functional currency loads **zero** FX rows and every conversion is an identity pass-through — so its figures and pixels are byte-for-byte unchanged (ADR-0099 §3, ADR-0101 §4). Each affected section gains a regression test mirroring `test_single_currency_tenant_sees_no_fx_surfaces`: identical output, and (where assertable) a zero-read proof matching the ADR-0101 phrasing.

### 6. Coordination with #041 and #042

- **#041** (`*_eur` → `*_functional` renames) touches the same DTO/field surface. This ADR's seam work **lands first**, carrying no renames, so the two diffs stay independent (one-concern rule). #041 proceeds afterward against the converted services.
- **#042** (live FX supply) is untouched and un-precluded: these services consume whatever rates `FxRateRepository.load_rates_frame` returns, regardless of supply path.

---

## Rationale

Converting levels is unarguable — a portfolio total in mixed nominal units is meaningless, and leaving Portfolio-Analysis weights inconsistent with the Review header is precisely the demo failure #040 exists to remove. The only substantive choice was return measurement (§1); functional-currency returns were chosen because (a) they are what a functional-currency investor actually experiences, (b) they are consistent with the FX-inclusive IRR/TVPI/DPI ADR-0099 §4 already ships, and (c) a single rule across all three services is simpler and less error-prone than per-statistic currency exceptions. The asset-vs-currency decomposition that a local-currency view would enable is a real analytical need, but it is an additive capability (attribution), deferred cleanly to #045 rather than bolted on as a parallel measurement basis now.

Reusing `build_portfolio_fx_converter` verbatim — rather than a new conversion helper — honours the ADR-0099 §4 single-boundary constraint: there is one conversion idiom in the codebase, and this ADR adds callers to it, not a variant of it.

---

## Alternatives Considered

- **Option B — Labelled nominal.** Keep the computation, add a "measured nominally across position currencies" disclosure on mixed-currency tenants. Rejected: cheap and honest for pure time-series statistics, but indefensible for composition weights and totals, which are simply wrong; and it would leave the Review/Statistics disagreement on screen with only a caveat.
- **Option C — Hybrid (convert levels, dual-track returns).** Convert level-based figures, offer vol/corr in both functional and position currency as an explicit hedged-vs-unhedged choice. Rejected *for now*: it is the most finance-complete answer, but the dual-track return view is exactly the attribution capability deferred to #045; shipping half of it (a raw local-currency vol) without the decomposition would be a confusing partial. Functional-only now, attribution later, is the cleaner sequence.
- **Local-currency returns only.** Rejected: would make returns and levels disagree on currency basis within the same surface, an internal inconsistency worse than the current cross-surface one.

---

## Consequences

### Positive
- The two halves of the application agree on every mixed-currency book; the demo shows one portfolio value everywhere.
- Shirley's `get_portfolio_statistics` and `get_saa_hypothetical_comparison` report functional-currency figures, consistent with the rest of the assistant's answers.
- One conversion idiom, reused; analytics purity intact; single-currency tenants provably unaffected.

### Negative
- Three services gain two constructor dependencies and a per-request converter build (one extra rate-frame read on mixed-currency tenants; zero on single-currency).
- Return statistics on foreign positions now differ from their previous (local) values on mixed books — correct, but a visible change for any existing mixed-currency tenant. Mitigated by the fact that the previous values were unlabelled-wrong.

### Neutral / Follow-ups
- **#045 raised:** FX / asset-return attribution (decompose functional-currency return into asset performance vs. currency effect) — the local-currency perspective lives here when built.
- **#041** proceeds after this seam work.
- Chart specs (`portfolio_analysis_frontier.py` and the benchmark/statistics specs) inherit converted inputs; their currency labelling may be revisited when #041 threads `functional_currency` through, but no spec logic changes here.

---

## Implementation Notes

Decomposed into three sequential Claude Code prompts (seam + unit tests per service is impractical to split three ways without churn; grouped by natural test boundary):

1. Conversion seams in all three services + analytics-purity re-verify + unit tests.
2. Route error-state wiring (three routes, error partials) + ASGI tests including the invisibility regression per section.
3. Documentation reconciliation (roadmap #040 → done, #045 raised; `architecture.md` services rows; ADR index `docs/adr/README.md`; CLAUDE.md if the contract glossary changes).

Claude Code authors no ADRs and runs no git.

---

## Compliance & Audit Relevance

Aligns the analytical surfaces with the functional-currency reporting basis already applied to the regulatory-facing Review and Limits engines, removing an inconsistency that would otherwise surface in any institutional review of the Statistics/SAA output. No change to limit or AnlV computation (already converted).

---

## References

- ADR-0099 (multi-currency model; §3 identity guarantee, §4 conversion boundary, §6 this deferral)
- ADR-0100 (explicit foreign-currency cash positions)
- ADR-0101 (FX exposure on the Overview; §4 invisibility invariant, zero-read proof)
- ADR-0013 (analytics-layer purity)
- ADR-0060 (NAV carry-forward, mirrored by FX carry-forward)
- ADR-0005 (typed exception hierarchy — `MissingFxRateError`)
- Roadmap #040 (this item), #041, #042, #045 (raised here)

---

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-07-11 | PortfoliFLOW project owner | Proposed. Successor to ADR-0099 §6. Functional-currency measurement for the statistics/SAA/benchmark layer; attribution deferred to #045. |
| 2026-07-11 | PortfoliFLOW project owner | Accepted against the shipped code. Implemented 2026-07-11: `PortfolioAnalysisService`, `StatisticsService`, and `BenchmarkComparisonService` convert at the ADR-0099 §4 boundary via `build_portfolio_fx_converter`; the three section routes render an HTTP-200 error partial on `MissingFxRateError`; analytics purity and the single-currency invisibility invariant preserved. |
