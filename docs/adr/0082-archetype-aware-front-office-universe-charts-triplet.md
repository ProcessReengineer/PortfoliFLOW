# ADR-0082: Archetype-Aware Front-Office Universe-Charts Triplet

- **Status:** Accepted — 2026-06-16
- **Date:** 2026-06-16
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** the Front-Office presentation layer of the
  liquid-asset archetypes (ADR-0079 §1), bringing mark-to-market surfaces to the
  universe-wide briefing
- **Related:** ADR-0073 (single-investment review — the sibling per-investment
  surface in Investor Communication), ADR-0079 (liquid-asset archetypes — schema
  and return conventions), ADR-0080 (historised composition-weight tables),
  ADR-0081 (liquid-archetype import format and sample data), ADR-0061 (benchmarks
  & attribution — the reused hero spec), ADR-0066 (cash-flow-adjusted returns),
  ADR-0013 / ADR-0045 (analytics-layer purity), ADR-0042 (Plotly web charting
  standard), ADR-0037 (FastAPI/Jinja/HTMX SSR), ADR-0067 / ADR-0072 (Front-Office
  Overview — a distinct surface)

---

## Context

The Front-Office universe-charts surface (`/front-office#charts`, sub-stream 6F-4)
renders one `<article>` per active investment, each lazy-loading a **three-chart
triplet** from `GET /api/charts/investment/{investment_id}`:

1. Total Return (NAV-rebased index),
2. Cashflows & NAV (the J-curve), and
3. TVPI / DPI / RVPI multiples.

All three are shaped by private-markets logic. Tiles 2 and 3 are
**economically meaningless for listed instruments** — a `listed_equity` or
`listed_bonds` fund has no capital calls, no J-curve, and no TVPI. With ADR-0081
now seeding the demo tenant with mark-to-market data (bond analytics, rating and
maturity ladders, dividend/coupon income), these funds currently route into the
private-markets triplet and produce NaN multiples (guarded) — the exact interim
incoherence ADR-0081 Follow-up #5 anticipated.

ADR-0079 §1 already decided the **presentation archetype** as the routing
concept, mapped from `investment_type`, and specified the analytics each
mark-to-market archetype needs — deliberately deferring those analytics and
chart-specs to implementation. ADR-0073 then applied an analogous archetype split
to the *single-investment review* in Investor Communication. What is still
missing is the equivalent decision for the **Front-Office universe-charts
triplet**: the surface that is a quick *universe scan* (one row of three charts
per holding), not the deep per-investment report.

This ADR records that decision. It is the Front-Office-charts sibling of ADR-0073
and the presentation-layer consumer of ADR-0079/0080/0081. It **re-decides none of
the ADR-0079/0080 schema** and adds no new data domain.

### Routing granularity — the chain that matters

Three taxonomies are in play and must not be conflated:

- **`asset_class`** — the operator-facing catalogue (twelve classes: equities,
  equities_em, gov_bonds_dm, ig_credit, hy_credit, private_equity, private_debt,
  infra_equity, infra_debt, real_estate, hedge_funds, cash).
- **`investment_type`** — the seven-value discriminator on `investments`
  (`private_equity, private_debt, real_estate, infra_equity, listed_equity,
  listed_bonds, other`).
- **archetype** — the presentation routing (ADR-0079 §1).

The triplet routes on the **archetype**, derived from `investment_type`. The
`asset_class` refines *within* an archetype, not across it: a govie, an IG, and a
HY fund all route to Fixed-Income and run through the same tile-set; their
rating-distribution tile simply renders different data. The chain is
`asset_class → investment_type → archetype`. Routing on the archetype (not
directly on the twelve-way asset-class) keeps one routing concept in the codebase
— the same map the single-investment review (ADR-0073) consumes.

### What exists and is reused

- The triplet template `web/templates/_partials/charts_investment_triplet.html`
  is **already a generic three-slot Plotly mount** — three `data-spec` targets
  with one bootstrap. Changing the archetype changes only the spec contents, not
  the template (the NAV-only fallback is the one shape exception).
- `build_benchmark_investment_total_return_spec` (ADR-0061) — the investment
  total-return-versus-benchmark spec, with the excess area already drawn. It is
  the **hero tile** for both mark-to-market archetypes.
- `build_nav_timeseries_spec` — the NAV time-series spec, reused **unchanged** for
  the NAV-only fallback.
- `compute_benchmark_comparison` (ADR-0061) — tracking error, information ratio,
  Sharpe for the KPI strip.
- The Statistics analytics block (max-drawdown, Sharpe/Sortino, VaR/CVaR, Ulcer,
  downside deviation, autocorrelation) for the KPI strip.
- `compute_cashflow_adjusted_return_series` (ADR-0066) — the single time-weighted
  total-return primitive for both listed archetypes, with `dividend`/`coupon`
  flows as the signed income (ADR-0079 §3).
- The per-investment composition repositories
  (`investment_sector_weights_repository.list_latest_for_investment`, region
  likewise) and the time-series FI reference repositories
  (`investment_bond_analytics_repository`, `investment_rating_weights_repository`,
  `investment_maturity_weights_repository`), all with
  `list_for_investment` / `list_latest_for_investment` reads.

### What is missing

A handful of analytics and chart-specs — exactly the set ADR-0079 §1 deferred to
implementation, scoped here to what the triplet needs — plus an archetype
resolver and a dispatch in the per-investment route. **Scenario/stress analysis
and cash-flow projection (Takahashi–Alexander) are explicitly out of scope** and
remain future work; this ADR evaluates only data already in the model.

---

## Decision

Six coordinated changes at the Front-Office universe-charts surface.

### 1. Archetype-aware routing for the triplet, four archetypes

`GET /api/charts/investment/{investment_id}` resolves the investment's archetype
from `investment_type` and dispatches to one of four tile-sets. The first three
are the ADR-0079 §1 archetypes; the fourth is an explicit fallback for `other`.

| Archetype | `investment_type` | Asset classes (typical) | Return logic |
|---|---|---|---|
| Capital-Account | `private_equity, private_debt, real_estate, infra_equity` | PE, Private Debt, Real Estate, Infrastructure Equity | money-weighted (existing) |
| Total-Return-Equity | `listed_equity` (and equity-like `other`) | Listed Equities (DM & EM) | time-weighted |
| Fixed-Income | `listed_bonds` | Govies, IG/HY Credit, Cash (degenerate) | time-weighted |
| NAV-only | `other` (non-equity-like) | Hedge Funds, Infra Debt, any class without a dedicated set | NAV only |

Cash routes to Fixed-Income as the degenerate case (ADR-0081 §3), not to a fourth
archetype. `other` routes to NAV-only **unless** it is explicitly equity-like, in
which case it joins Total-Return-Equity (ADR-0079 §1).

### 2. The four tile-sets

**Capital-Account — unchanged.** Tiles: Total-Return Index · Cashflows & NAV ·
TVPI/DPI/RVPI. KPI caption: TVPI · DPI · Net IRR · Unfunded Commitment. No new
work beyond routing; the three existing specs (`build_total_return_spec`,
`build_cashflows_nav_spec`, `build_multiples_spec`) are retained verbatim.

**Total-Return-Equity — new.** Tiles:

1. **TR-Index vs. Benchmark** — `build_benchmark_investment_total_return_spec`,
   the fund line, the mapped benchmark line, and the shaded excess.
2. **Underwater / Drawdown** — an underwater plot computed on the time-weighted
   TR series (running peak to trough).
3. **Sector & Region** — one tile holding **two separated breakdowns** (sector
   left, region right; divider between), never merged into one.

KPI caption: 1Y TWR · Volatility · Sharpe · benchmark-relative Beta · Tracking
Error · Information Ratio (trailing TWR 1M/3M/YTD/1Y/3Y/SI available in the caption
payload).

**Fixed-Income — new.** Tiles:

1. **TR-Index vs. Benchmark** — same hero spec as Total-Return-Equity.
2. **YTM/OAS & Duration** — a dual-axis time series from
   `investment_bond_analytics` (YTM and, where present, OAS on one axis; effective
   duration on the other).
3. **Rating & Maturity** — one tile holding **two separated breakdowns** (credit-
   rating distribution left; maturity ladder right; divider between).

KPI caption: TWR · YTM · Effective Duration · notch-weighted Ø Rating · OAS.

**NAV-only — new, minimal.** A single wide NAV tile (`build_nav_timeseries_spec`,
actual with plan overlay where present) plus a neutral note that archetype-specific
charts will follow. No KPI strip. The investment stays **visible** in the universe
scan rather than silently producing meaningless private-markets tiles.

### 3. Risk metrics live in a KPI caption, not a fourth tile

The triplet is the *universe scan*; three charts is its budget. The full
risk-tile treatment (a dedicated risk block, monthly-return heatmap, distribution
history) belongs to the **single-investment review** (ADR-0073 / ADR-0079 §1), a
separate and deeper surface. On the triplet, the risk and characteristic figures
are rendered as a compact KPI caption strip beneath the three tiles, recombining
existing analytics. This keeps the two surfaces distinct: scan here, deep-dive
there.

### 4. Composition tiles carry two separated breakdowns

Both the Total-Return-Equity composition tile (sector | region) and the
Fixed-Income composition tile (rating | maturity) render **two distinct mini-charts
within one tile**, divided, never a single merged breakdown. This is a deliberate
read-separability requirement: a portfolio manager must read sector independently
of region, and credit quality independently of maturity. Equity sector/region
remains a point-in-time snapshot (ADR-0081 Follow-up #4); the FI rating and
maturity ladders are time-series but render at their latest `as_of_date` on the
triplet.

### 5. New analytics and chart-specs (the ADR-0079-deferred set, triplet-scoped)

Analytics (pure, DataFrame-in/DataFrame-out, `services/analytics/`, guarded by
`tests/regression/test_analytics_layer_pure.py`):

- an underwater / running-peak drawdown series (the level series; the scalar
  max-drawdown already exists),
- a notch-weighted average rating (buckets → notches → weighted mean → bucket),
- benchmark-relative beta (the one genuinely new figure ADR-0079 §1 named),
- a trailing-TWR table (1M/3M/YTD/1Y/3Y/SI) and rolling 12-month volatility/Sharpe
  for the KPI caption.

Chart-specs (pure, themed, Plotly, `services/chart_specs/`):

- an underwater spec,
- per-investment sector and region composition specs (one spec parameterised by
  dimension, or two siblings),
- a YTM/OAS-and-duration dual-axis spec,
- a rating-distribution spec and a maturity-ladder spec.

The hero (`build_benchmark_investment_total_return_spec`) and the NAV-only spec
(`build_nav_timeseries_spec`) are reused as-is.

### 6. Resolver, route, and template

- An **archetype resolver** (`investment_type → archetype`) is the single seam
  encoding the §1 map; the single-investment review may later consume the same
  resolver.
- `get_charts_investment` resolves the archetype, assembles the per-archetype
  bundle and KPI payload via `InvestmentService`, and selects the spec set.
- The generic triplet template renders the three-spec archetypes unchanged; the
  NAV-only archetype renders a single wide tile (template variant). A KPI-caption
  partial is added beneath the tiles.
- `GET /api/charts/section` passes the resolved archetype per article (the article
  skeleton can label the archetype without fetching chart data).

---

## Alternatives Considered

- **Route directly on the twelve-way `asset_class`.** Rejected — it duplicates the
  archetype concept ADR-0079 already established, multiplies presentation surfaces
  beyond what the data model distinguishes (e.g. nothing in the model separates a
  govie tile-set from an IG tile-set), and breaks the single-routing-concept reuse
  with the single-investment review. `asset_class` refines *within* an archetype.

- **A fourth risk tile per mark-to-market archetype (a 2×2 set).** Rejected — it
  breaks the universe-scan budget and overlaps the single-investment review, which
  is the deep risk surface. Risk figures go in the KPI caption instead.

- **An Infrastructure-specific set distinct from Private Equity.** Rejected for now
  — the model carries no infra-specific fields (earlier-yield, distribution-rate),
  so a distinct set would have no distinct data to show. PE, PD, RE and Infra share
  Capital-Account, exactly as ADR-0079 grouped them (YAGNI).

- **Per-archetype templates.** Rejected — the existing triplet template is already
  a generic three-slot Plotly mount; only spec contents differ. The single shape
  exception is NAV-only (one tile), handled as a small template variant rather than
  a parallel template family.

- **Merge sector+region (or rating+maturity) into one stacked breakdown per tile.**
  Rejected — the operator must read the two dimensions independently; the tile holds
  two divided panels.

- **Build the deep single-investment review now.** Rejected — that is ADR-0073's
  separate surface in Investor Communication with its own (richer) tile budget and
  the still-open portfolio-level TWR/IRR aggregation question. This ADR scopes the
  Front-Office *scan* only.

---

## Consequences

### Positive

- `listed_equity` and `listed_bonds` get factsheet-grade scan tiles instead of NaN
  multiples; the ADR-0081 interim incoherence (Follow-up #5) is resolved at the
  Front-Office surface.
- The change is reuse-heavy: the hero spec, the NAV-only spec, the benchmark and
  Statistics analytics, the composition repositories, and the generic triplet
  template are all reused. New work is the small deferred analytics/spec set plus a
  resolver and a dispatch.
- One routing concept — the archetype resolver — is shared with the eventual
  single-investment review, avoiding a second `asset_class`-keyed routing scheme.
- Analytics-layer purity (ADR-0013/0045) and the web-layer import discipline
  (ADR-0042; no matplotlib, no `PersistentDataStore` under `web/`) are preserved.
- `other` instruments stay **visible** in the universe scan via NAV-only, so unsupported
  asset classes are an explicit, reviewable state rather than a silent gap — the team
  decides from live data where to add a dedicated set next.

### Negative

- A handful of new analytics functions and chart-specs, plus a new archetype-resolver
  seam and a route dispatch, with their tests.
- The Total-Return-Equity and Fixed-Income composition tiles are visually denser
  (two panels in one tile) than the single-chart Capital-Account tiles. Accepted as
  a deliberate read-separability trade.
- The surfaces render only against ADR-0081 sample data; until the v26 workbook and
  its seeded generator are in the demo tenant, the mark-to-market tiles render empty
  states (see Tests, empty-data).

### Neutral

- **Portfolio-level archetype-mixed aggregation remains out of scope** (ADR-0079
  Neutral): the triplet is per-investment, so a blended money-/time-weighted rule is
  not needed here. It is still its own successor ADR.
- **Front-Office Overview archetype-awareness** (ADR-0067/0072 KPI strip) is a
  distinct, later step — a mixed portfolio cannot show one universal multiples set,
  and that redesign is pulled separately.
- **Scenario/stress analysis and cash-flow projection** are explicitly future work
  and untouched here.
- Whether the equity sector/region snapshot should be historised to match the FI
  weight tables stays open (ADR-0081 Follow-up #4).

---

## Tests

1. **Archetype routing.** Each `investment_type` resolves to the intended
   archetype; the `listed_bonds` triplet never contains a TVPI/J-curve spec; the
   `private_equity` triplet never contains a YTM/duration spec; `other`
   (non-equity-like) yields the single NAV tile (mirrors ADR-0079 Test 5 at the
   Front-Office surface).
2. **Spec presence per archetype.** The dispatch returns exactly the intended spec
   set for each archetype (three specs for the first three, one for NAV-only) plus a
   KPI payload — asserted on the route response.
3. **Composition tiles carry two distinct breakdowns.** The equity composition tile
   exposes both a sector and a region breakdown; the FI composition tile exposes both
   a rating and a maturity breakdown — never one merged series.
4. **Analytics purity.** The new underwater, notch-weighted-rating, beta and
   trailing/rolling functions pass `test_analytics_layer_pure`.
5. **Notch-weighted rating.** A known rating distribution maps to the expected
   notch-weighted average; a naive mean differs and the difference is detected.
6. **Empty-data, no silent fallback.** A liquid investment with no `bond_analytics`
   rows renders a neutral empty state (HTTP 200), not a crash and not a fabricated
   zero — the missing input is visible.
7. **Web-layer discipline.** `test_no_matplotlib_in_web` and
   `test_web_does_not_import_persistent_data_store` remain green.

---

## Compliance & Audit Relevance

- **Tenant isolation unchanged.** Both endpoints run inside `tenant_context` with
  `require_session`; cross-tenant ids return `None` (RLS hides the row) and render a
  neutral state without leaking row existence (ADR-0035, ADR-0078).
- **Read-only.** No data is written; no audit-log entries are generated.
- **Layer purity preserved.** The surface imports only `services.chart_specs`
  (Plotly, matplotlib-free per ADR-0042) and the existing service; no `matplotlib`
  or `PersistentDataStore` import is added under `web/`.
- **Provenance seam respected.** The `basis` discriminator (`reported` vs future
  `computed`) on the FI reference data is carried through unchanged; the triplet
  reads it but introduces no new behaviour on it (ADR-0079 forward seam).
- **ISO 25010.** Functional Suitability (mark-to-market instruments gain correct
  surfaces); Compatibility (the Capital-Account set and the generic template are
  untouched); Reliability (empty-data states rather than crashes; no silent
  fallback).

---

## References

- ADR-0079 (liquid-asset archetypes — the §1 archetype map and the deferred
  analytics/spec list this ADR implements at the Front-Office surface)
- ADR-0073 (single-investment review — the sibling per-investment archetype surface
  in Investor Communication; the deeper risk-tile view)
- ADR-0080 (historised composition-weight tables — the FI rating/maturity series)
- ADR-0081 (liquid-archetype import format and sample data — the data dependency)
- ADR-0061 (benchmarks & attribution — the reused hero spec and the TE/IR/Sharpe
  analytics)
- ADR-0066 (cash-flow-adjusted returns — the reused time-weighted TR primitive)
- ADR-0067 / ADR-0072 (Front-Office Overview — the distinct KPI-strip surface whose
  archetype-awareness is a separate later step)
- ADR-0042 (Plotly as the web charting standard, matplotlib reserved)
- ADR-0037 (FastAPI/Jinja/HTMX SSR)
- ADR-0013 / ADR-0045 (analytics-layer purity)
- Successor, still open: portfolio-level TWR/IRR aggregation for mixed-sleeve
  portfolios (ADR-0079 Neutral)

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-06-16 | PortfoliFLOW project owner | Initial decision. |
