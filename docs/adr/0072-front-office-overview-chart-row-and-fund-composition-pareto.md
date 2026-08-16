# ADR-0072: Front Office "Overview" — Chart Row and Fund-Composition Pareto

- **Status:** Accepted
- **Date:** 2026-06-03
- **Deciders:** PortfoliFLOW project owner
- **Tags:** frontend, ui, web, htmx, front-office, overview, charts, plotly, analytics, phase-6

---

## Context

The Front Office "Overview" section (ADR-0067) currently opens with a hero AUM figure and a
four-card metric grid (IRR / TVPI / DPI / active-investment count). It gives the reader the
*numbers* of the portfolio but no immediate *graphical* impression before the long-scroll
moves on to the Charts section and the per-investment detail. For the target audience —
boutique fund-of-funds managers and institutional allocators — a portfolio-level visual
orientation directly under the headline figures is the natural next beat.

The Overview already consumes `PortfolioReviewService.get_portfolio_overview()` (via
`FrontOfficeOverviewService`), which returns a `PortfolioOverviewBundle` carrying, among
other things, the year-end `invested_capital_nav` and `cashflows` series. Two of the
portfolio-aggregate tiles rendered in the Investor-Communication Portfolio Review surface —
its top-left "Invested Capital & NAV" and its top-middle "Cashflows" — are built by the pure
spec functions `build_invested_capital_nav_spec` and `build_yearly_cashflows_spec` from
exactly these series. Surfacing them in the Overview is therefore a reuse, not a
re-computation: numerical identity with the Portfolio Review holds by construction (the same
argument that ADR-0067 made for the KPI strip).

The third chart needs a fresh decision. The intent is a portfolio-composition view — how the
book is split across the funds it contains. The obvious encodings each have a problem at this
position:

- A **pie chart** is space-inefficient and, with the demo universe at 16 funds, dissolves
  into unreadable slivers; it is also a visual cliché.
- A **donut** is the same data with a hole; it reads acceptably only at low category counts
  (≈3–6 clean categories), which is not the fund-level case (16 funds).
- A **treemap** is already the encoding used twice in the Portfolio Review six-tile set
  (Region split, Sector split). Reusing it a third time on the headline surface would make
  the application's visual vocabulary repetitive.

What the position actually wants is a part-to-whole view that (a) is legible at 16+
categories, (b) is visually distinct from the two time-series charts beside it and from the
treemaps elsewhere, and (c) carries decision-relevant signal for a fund-of-funds — namely
**concentration**: how much of the book sits in the largest few funds. That last point ties
directly into the Investment-Limits / Anlagegrenzen work, where concentration is a
first-class concern.

---

## Decision

Add a **three-chart row** to the Front Office "Overview" section, rendered directly under the
metric grid and before the Charts section. The row contains, left to right:

1. **Invested Capital & NAV** — reuses `build_invested_capital_nav_spec(bundle.invested_capital_nav)`.
2. **Cashflows** — reuses `build_yearly_cashflows_spec(bundle.cashflows)`.
3. **Portfolio composition** — a new **horizontal Pareto chart** of NAV by fund.

### The third chart: a horizontal NAV-by-fund chart with an IRR overlay (revision 1.3)

The composition tile is a sorted **horizontal bar** chart — one bar per fund, **absolute NAV
in EUR** on the bottom x-axis, fund name on the (reversed) categorical y-axis so the largest
fund sits at the top. The secondary top x-axis no longer carries a cumulative NAV-share line;
it now carries **per-fund IRR-since-inception as markers** (dots), and **concentration is
surfaced as a compact statistics strip** beneath the title rather than as a curve.

- **The second axis carries performance, not concentration.** Revision 1.1 overlaid a
  cumulative NAV-share line on a secondary top axis. On a *sorted, reversed-horizontal* Pareto
  the cumulative curve runs top-to-bottom, which the eye does not parse as concentration, and
  on already-sorted bars the concentration is largely legible from the bar lengths alone — so
  the line was visually redundant while its only precise contribution (the "top-N = X %" read)
  was exactly the figure hardest to extract from a downward curve against a top axis. The
  second axis now carries a genuinely **independent** dimension: each fund's IRR-since-inception,
  drawn as a dot positioned on a 0-anchored percent axis. Bar length encodes *size*, dot
  position encodes *performance* — the decision-relevant cross-tab for a fund-of-funds ("are the
  largest positions also the strongest?").
- **No connecting line between the IRR dots.** The funds are ordered by NAV, not by IRR, so a
  line joining the dots would imply an ordering that does not exist. The IRR series is therefore
  markers-only.
- **Funds without a convergent IRR carry no dot.** Where the root-finder cannot converge
  (`irr is None`), the fund simply has no marker on the IRR axis (replacing the em-dash sentinel
  of the former text column). The bar and its NAV label are unaffected.
- **The IRR text column is removed.** The per-fund IRR-since-inception that revision 1.1 rendered
  as a right-hand tabular annotation is now the dot series; the redundant text column is dropped,
  freeing the right margin and removing the brand's only off-bars text block.
- **Concentration becomes a statistics strip.** A compact, single-line subtitle beneath the
  title reports `Top 3 · X%  |  Top 5 · Y%  |  Top 10 · Z%  |  HHI h`. These are computed on the
  **full, ungrouped** fund distribution (see "New analytics aggregation" below) — never on the
  top-N + "Other" view, whose single tail bucket would distort the HHI. The strip is rendered in
  the theme's `neutral` colour so it reads as metadata, not as a fourth data series.
- **The "Other" bar is drawn in the theme `neutral` colour.** The top-N grouping (unchanged from
  revision 1.1) still folds the tail into one `"Other (k funds)"` bar. That bar is now filled in
  `colours["neutral"]` rather than the accent `colours["primary"]`, so the residual aggregate is
  visually distinct from the individual funds and is not misread as the single largest holding
  (which, at the demo universe, it can outweigh). This keeps the single-accent crimson reserved
  for genuine per-fund positions. The per-fund bar colour is unchanged.
- **Top 10 individually, remainder as "Other"** and **"Other" IRR as a NAV-weighted average**
  are retained verbatim from revision 1.1.

### New analytics aggregation (added in revision 1.3)

A new pure function `compute_concentration(breakdown) -> ConcentrationStats` is added to
`services/analytics/portfolio_aggregation.py`. It consumes the **full** (ungrouped)
`FundCompositionBreakdown` and returns a frozen `ConcentrationStats` carrying `top1_pct`,
`top3_pct`, `top5_pct`, `top10_pct` (cumulative NAV share of the largest 1 / 3 / 5 / 10 funds,
each clamped to 100 % when fewer funds exist), `hhi` (the Herfindahl–Hirschman index as the sum
of squared NAV fractions over **all** funds, on a 0..1 scale), and `fund_count`. It is DB-free,
Qt-free and FastAPI-free, in keeping with the analytics-purity contract (ADR-0013 / ADR-0045 §3).
The Overview route computes it on the ungrouped `fund_composition` and passes it to the spec; the
top-N grouping for the bars stays a separate, presentation-only step.

The chart spec `build_fund_composition_spec` gains an optional `concentration: ConcentrationStats
| None = None` parameter. When supplied (and `fund_count > 0`) it renders the strip; when omitted
the spec stays valid and strip-free, so existing non-route callers are unaffected.

### New analytics aggregation

A new pure function `aggregate_fund_composition(investments, nav_by_investment)` is added to
`services/analytics/portfolio_aggregation.py`, returning a frozen
`FundCompositionBreakdown(rows: list[FundCompositionRow])`. Each row carries
`investment_id`, `name`, `nav_eur`, `weight_pct` and the running `cumulative_pct`. Funds with
non-positive NAV are skipped; rows are sorted by NAV descending; the total weight sums to
~100 %. The function is DB-free, Qt-free and FastAPI-free, in keeping with the analytics-purity
contract (ADR-0013 / ADR-0045 §3).

The breakdown is attached to the existing `PortfolioOverviewBundle` as a new
`fund_composition` field with a safe default (`FundCompositionBreakdown(rows=[])`), populated in
`get_portfolio_overview` from the `nav_by_inv` mapping already in scope. The
`SingleInvestmentReviewBundle` is **not** extended — fund composition of a single investment is
trivially 100 % and carries no information. The Portfolio Review tile set stays at its six
tiles and ignores the new field.

### Service and route wiring

`FrontOfficeOverviewService` gains a single bundle-fetching entry point
`get_overview(as_of_date=None) -> OverviewResult | None`, where `OverviewResult` bundles the
existing `OverviewKpis` together with the three chart inputs
(`invested_capital_nav`, `cashflows`, `fund_composition`). The existing
`get_overview_kpis(...)` is retained as a thin wrapper that returns `result.kpis`, so all
current callers — including the Shirley overview analysis tool — are unaffected.

The Overview route (`web/routes/overview.py`) calls `get_overview()` once, builds the three
Plotly specs, and passes them to the template as an `overview_charts` list of tile dicts
(`{id, title, spec, has_data}`), mirroring the Portfolio Review `tile_specs` shape. The template
renders them with the established `.pf-plotly-target[data-spec]` pattern and a small inline
`Plotly.newPlot` reveal script (the Overview section is HTMX-swapped via `outerHTML`, so it
carries its own renderer, exactly as the Portfolio Review section body does).

### Naming

The new spec module is `services/chart_specs/portfolio_fund_composition.py`, exporting
`build_fund_composition_spec`. The neutral `portfolio_` prefix (rather than
`front_office_` or `portfolio_review_`) reflects that the function is a surface-agnostic
portfolio-level encoding; the Overview is merely its first consumer.

---

## Rationale

Reusing the two existing Portfolio Review specs is the load-bearing choice for the left and
middle charts: it imports zero new analytics and guarantees the two surfaces cannot diverge
numerically. For the third chart, the Pareto encoding is the only one of the candidates that
*improves* rather than degrades as the fund count rises, is visually distinct from everything
else on the page, and surfaces concentration — the signal that matters most for the audience
and that connects to the limits work. Attaching the new breakdown to the existing bundle keeps
the aggregation pure and computed once per request, and routing it through a single
`get_overview()` entry point avoids a second, redundant bundle fan-out while leaving the
public `get_overview_kpis` contract intact.

Revision 1.3 makes the concentration figure that motivated this tile in revision 1.0/1.1
*explicit and exact* (top-N share and HHI as numbers) rather than implicit in a curve, which
connects more directly to the Investment-Limits / Anlagegrenzen work where concentration is a
first-class, threshold-bearing concern. Encoding IRR on the freed second axis turns the tile
from a single-signal concentration view into a size-versus-performance view at no extra ink.

---

## Alternatives considered

- **Donut on the fund dimension (the owner's fallback).** Rejected for 16 funds: thin slivers,
  a long grey "Other" tail, and no concentration read. A donut remains a good fit for a
  *low-cardinality* dimension and is explicitly preserved for a possible future Strategy /
  Asset-Class composition surface (see below).
- **Strategy / Asset-Class composition instead of fund-level.** A clean donut would shine here
  (≈3–6 categories), and the data is available (`investment.asset_class_id`,
  `investment.investment_type`). Deferred: the owner intends to give strategy / asset-class its
  own dedicated Front Office surface with proper drill-downs rather than fold it into the
  headline glance. Recorded as a future direction, not v1 scope.
- **Treemap on the fund dimension.** Rejected to avoid a third treemap; the encoding is already
  the Region-split and Sector-split tiles in Portfolio Review.
- **Single 100 %-stacked horizontal bar.** The most literal "composition" and the most compact,
  but a short-wide element reads awkwardly between two tall time-series tiles in a three-up row,
  and 16 segments are hard to label. Rejected on layout and legibility grounds.
- **Vertical Pareto.** The textbook orientation, but at 16 funds the x-axis fund-name labels
  truncate in the tile height. Rejected in favour of the horizontal variant for name
  legibility on a glance surface.
- **Cash-flow-correct pooled IRR for the "Other" bucket.** The mathematically clean residual
  IRR would re-aggregate the tail funds' cashflows and solve a single IRR. Deferred: it would
  force the grouping into the aggregation step (where the per-fund cashflow series live) rather
  than a thin row-level grouping helper, for marginal benefit on a tail bucket. The
  NAV-weighted average of the tail IRRs was chosen instead (project owner's call).
- **Per-fund NAV *share* bars (revision 1.0).** Superseded: redundant with the cumulative line
  because both shared the percent scale. Replaced by absolute-EUR bars in revision 1.1.

---

## Consequences

### Positive

- The Overview opens with an immediate, portfolio-level graphical impression in addition to
  the headline figures.
- The left and middle charts are numerically identical to the Portfolio Review by construction
  (shared specs, shared bundle).
- The composition tile doubles as a concentration read, reinforcing the institutional framing
  and foreshadowing the Anlagegrenzen surface.
- The analytics-purity, RLS and "no matplotlib / no persistent-data-store in web" regression
  guards are unaffected: the new aggregation is pure, and no new database access path is
  introduced (the data is read from the existing bundle).

### Negative

- A new chart-spec generator and a new aggregation enter the codebase and must be unit-tested
  (trace structure, axis wiring, ordering, cumulative monotonicity, empty / tail-grouping
  cases).
- `FrontOfficeOverviewService` gains a second public method and a result dataclass. Mitigated by
  keeping `get_overview_kpis` as a delegating wrapper, so existing callers are untouched.

### Neutral

- v1 ships without an as-of-date control (consistent with ADR-0067); the resolved
  latest-activity date drives all three charts so they share one as-of.
- The composition spec supports optional tail-grouping (`max_bars`) but defaults to showing all
  funds; the demo universe (16) is comfortably within the legible range.

---

## Implementation pointers

- **Analytics:** `services/analytics/portfolio_aggregation.py` — add `FundCompositionRow`,
  `FundCompositionBreakdown`, `aggregate_fund_composition`; extend `__all__`.
- **Bundle:** `services/portfolio_review/portfolio_review_service.py` — add the
  `fund_composition` field (with default) to `PortfolioOverviewBundle`; populate it in
  `get_portfolio_overview`.
- **Spec:** `services/chart_specs/portfolio_fund_composition.py`
  (`build_fund_composition_spec`); register in `services/chart_specs/__init__.py` + `__all__`.
- **Service:** `services/front_office_overview/overview_service.py` — add `OverviewResult` and
  `get_overview`; refactor `get_overview_kpis` to delegate; extend `__init__.py` re-exports and
  `__all__`.
- **Route:** `web/routes/overview.py` — call `get_overview`, build the three specs, pass
  `overview_charts` into the context.
- **Template:** `web/templates/_partials/overview_section.html` — add the `.ov-charts` block and
  the inline reveal script (mirroring `portfolio_review_section.html`).
- **CSS:** `web/static/css/components/overview.css` — add `.ov-charts` / `.ov-chart*` rules
  mirroring `.pr-grid` / `.pr-tile*`. No `base.html` change (Plotly and `overview.css` are
  already linked).
- **Tests:** analytics unit tests for `aggregate_fund_composition`; chart-spec structural tests
  for `build_fund_composition_spec`; an extension of `tests/web/test_overview_section_routes.py`
  asserting three chart targets in the seeded-universe response.

---

## Compliance and audit relevance

**Low.** The decision is additive at the presentation and orchestration tiers plus one new pure
analytics function. No repository, RLS policy, or persistence entry point changes; no new
cross-tenant access path is introduced (the composition is derived from NAVs already loaded
under the active tenant context). The two reused charts are built from series already
characterised against the QT reference, and the new aggregation is a deterministic
NAV-weighting with no external dependency. The `services/` purity contract (DB-free,
FastAPI-free, Qt-free) and the matplotlib-free invariant of `services/chart_specs/` are
preserved. The decision is non-confidential and documented for traceability.

---

## Related ADRs

- ADR-0067 — Front Office "Overview" KPI strip (the section this extends)
- ADR-0073 — Single-Investment Review web surface / Portfolio Review six-tile set (source of the
  two reused specs and the `.pf-plotly-target` render pattern; renumbered from 0069 on 2026-06-03)
- ADR-0055 — Cash as Residual in AUM Coverage (the AUM / invested / NAV semantics the Overview
  honours)
- ADR-0045 — Charts/Statistics web migration and analytics-service foundation (the
  `PortfolioReviewService` / analytics split this reuses)
- ADR-0042 — Charting architecture (Plotly specs, matplotlib-free `services/chart_specs/`)
- ADR-0021 — Chart theming externalised to JSON (the canonical palette the new spec consumes)
- ADR-0013 — Analytics layer pure and stateless (the contract the new aggregation observes)
- ADR-0001 — Layered architecture (route → service → analytics, one-way dependencies)

---

## Revision history

| Date       | Revision | Note                                                      |
| ---------- | -------- | --------------------------------------------------------- |
| 2026-06-03 | 1.0      | Initial Accepted status; authored before implementation.  |
| 2026-06-03 | 1.1      | Bars switched to absolute EUR; per-fund IRR text column added; top-10 + "Other" grouping moved into a pure `group_fund_composition` helper with NAV-weighted "Other" IRR. |
| 2026-06-03 | 1.2      | Target-audience framing broadened by ADR-0074 (product scope: institutional portfolio management). Body unchanged. |
| 2026-06-03 | 1.3      | Replaced the cumulative NAV-share line on the secondary axis with a per-fund IRR marker series (markers-only; funds with no IRR carry no dot); removed the IRR text column; surfaced concentration as a `neutral`-coloured Top-3/5/10 + HHI statistics strip computed on the full ungrouped distribution via the new pure `compute_concentration` / `ConcentrationStats`; drew the "Other" residual bar in `colours["neutral"]`. Second-axis dimension changed from concentration to performance. |
