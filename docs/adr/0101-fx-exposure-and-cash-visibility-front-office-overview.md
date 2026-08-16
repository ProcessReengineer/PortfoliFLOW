# ADR-0101: FX Exposure and Cash Visibility on the Front-Office Overview

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** PortfoliFLOW project owner
- **Tags:** front-office, overview, charts, fx, currency, ui, phase-8
- **Depends on:** ADR-0099 (functional currency, conversion boundary),
  ADR-0100 (explicit cash positions), ADR-0067 (KPI strip),
  ADR-0072 (Overview chart row)

---

## Context

Blocks 1–4 of the multi-currency programme made portfolio aggregates
functional-currency correct (ADR-0099 §4) and made foreign-currency
cash balances first-class investment rows (ADR-0100). What does not
yet exist is **visibility**: the Front-Office Overview shows correctly
converted totals but nothing about *what* is exposed to *which*
currency, and an explicit `Cash USD` position is indistinguishable
from a fund in every overview surface. For the target audience
(Versorgungswerke and family offices with AnlV-adjacent reporting
duties), unhedged FX exposure by position currency is a standing
supervisory and committee question.

Two presentation seams exist and are load-bearing precedents:

- the ADR-0067 KPI strip (`OverviewKpis`, scalars only, formatting in
  the route),
- the ADR-0072 chart row (`OverviewResult` bundles per-chart inputs;
  specs in `services/chart_specs/`; pure aggregation in
  `services/analytics/portfolio_aggregation.py`, cf.
  `aggregate_fund_composition`).

Additionally, the route's `_format_eur_compact` hard-codes the `€`
symbol and several templates hard-code EUR wording, while the bundle
already carries `functional_currency` (Block 3, unused by the UI so
far).

## Decision

### 1. Currency-exposure chart as the fourth Overview chart tile

A pure aggregation `aggregate_currency_exposure(investments,
nav_by_investment)` in `services/analytics/portfolio_aggregation.py`
groups the **converted** latest NAVs (full universe, cash included)
by **position currency** and returns a `CurrencyExposure` DTO
(per currency: functional-currency amount and percentage share;
shares sum to 100). The exposure measured is *unhedged notional NAV
share by denomination* — the supervisory default view.

A `build_currency_exposure_spec` in `services/chart_specs/` renders
it as a donut on the chart-theme (crimson primary via CSS variable,
never hardcoded hex), following the fund-composition spec idiom. The
tile joins the ADR-0072 row as `ov-chart-currency`.

**Conditional rendering:** the tile renders only when more than one
position currency is present in the universe.

### 2. FX-cash card

`OverviewResult` gains `cash_positions: list[CashPositionRow]` —
one row per ADR-0100 cash investment whose position currency differs
from the functional currency, carrying: display name, position
currency, **native** balance (position currency), functional-currency
value, and the NAV as-of date the balance was observed. The implied
rate (functional ÷ native) is derivable in presentation; no converter
API change.

The native balance is captured at the ADR-0099 §4 seam in
`PortfolioReviewService` as a side-map of pre-conversion latest NAVs
— a read of a value the seam already holds, **not** a second
conversion path.

A compact card renders these rows in the Overview (placement
alongside the ADR-0067 grid; one line per currency balance: native
amount, functional equivalent, as-of). **Conditional rendering:** the
card appears only when at least one such row exists.

### 3. Currency-aware money formatting and label threading

`_format_eur_compact` generalises to
`_format_money_compact(value, currency)`: `€`/`$`/`£` symbols for
EUR/USD/GBP, ISO-code prefix (`CHF 1.2M`) otherwise; thresholds and
rounding unchanged. Overview and Portfolio-Review surfaces thread
`functional_currency` from the bundle into money labels where `EUR`
or `€` is currently hard-coded (a label audit accompanies the
change). Field names (`aum_eur`, `nav_eur`, …) remain — the rename
refactor stays a separate follow-up (ADR-0099 §Follow-ups).

### 4. Single-currency invisibility guarantee

For a tenant whose entire universe is denominated in the functional
currency, the Overview renders **identically to today**: no exposure
tile, no FX-cash card, unchanged labels (when the functional currency
is EUR). This is the presentation-layer mirror of the Block-1/3
zero-read guarantee.

## Rationale

- Exposure-by-denomination and explicit FX cash are the two questions
  the multi-currency model exists to answer; the Overview is the
  decision surface where they belong (ADR-0067's premise).
- Reusing the pure-aggregation → chart-spec → route idiom keeps
  analytics purity intact and the change surface small; the exposure
  function is a sibling of `aggregate_fund_composition`.
- Conditional rendering keeps single-currency tenants visually
  untouched — continuity of trust with Blocks 1–4's zero-impact
  guarantees.
- The native-balance side-map avoids re-deriving position-currency
  values downstream of a seam whose entire purpose is that they no
  longer circulate.

## Alternatives Considered

- **Exposure as a KPI-strip scalar** (e.g. "FX share 23 %"):
  rejected — exposure is distributional; a single scalar hides the
  which-currency answer the committee asks next.
- **A dedicated FX page/area:** rejected for now — the Overview is
  the established decision surface; a page would fragment it for two
  tiles' worth of content. Revisit when hedging arrives.
- **Hedged/unhedged exposure columns:** out of scope — hedging is
  deferred (ADR-0099 §6); the donut states unhedged notional and
  says so in its subtitle.
- **Converting labels via full `*_eur` rename now:** rejected —
  bundling a cross-cutting rename into a UI block violates the
  one-concern rule; labels can be correct without the rename.

## Consequences

### Positive

- FX exposure and FX cash become visible exactly where allocation
  decisions are reviewed; supervisory questions get a screen.
- Non-EUR functional currencies stop being mislabelled as `€`.

### Negative

- One more chart tile widens the ADR-0072 row's responsive surface
  (grid wrap must be verified at narrow widths).
- The exposure donut shows notional denomination, not economic
  exposure (look-through of funds' underlying currencies is out of
  scope) — the subtitle must say "by position currency" to preempt
  misreading.

### Neutral / Follow-ups

- `*_eur` → `*_functional` rename (unchanged follow-up).
- Hedged-exposure view when hedging lands.
- PDF reporting (#001) inherits the exposure aggregation when built.

## Implementation Notes

- `aggregate_currency_exposure` + DTO next to
  `aggregate_fund_composition`; pure, tested against hand-computed
  shares.
- `build_currency_exposure_spec` in `services/chart_specs/`; theme
  via `config/chart_theme.json` CSS variables.
- Seam-A side-map for native cash balances; `CashPositionRow` on
  `OverviewResult`; conditional template blocks in
  `overview_section.html`.
- `_format_money_compact(value, currency)` with threshold tests per
  symbol branch; label audit recorded in the commit body.
- Route/template tests: single-currency invisibility; mixed-currency
  render (tile + card present, shares sum to 100, native +
  functional agree with the fixture rates).

## Compliance & Audit Relevance

- **ISO 25010:** Functional appropriateness (exposure reporting),
  Usability (denomination visible at the decision surface).
- **Anlagegrenzen adjacency:** the exposure donut is informational;
  quota enforcement remains the limits engine's job — no double
  authority.

## References

- ADR-0067 (Overview KPI strip), ADR-0072 (chart row +
  fund-composition idiom), ADR-0082 (chart-triplet precedent)
- ADR-0099 §§3–4, §6 (conversion boundary, deferrals)
- ADR-0100 §§3–4 (cash rows, residual, engine contracts)

---

## Revision History

| Date       | Author                     | Change         |
|------------|----------------------------|----------------|
| 2026-07-11 | PortfoliFLOW project owner | Initial draft. |
| 2026-07-11 | PortfoliFLOW project owner | Accepted against the shipped code. Implemented 2026-07-11: the currency-exposure donut tile, the FX-cash card, and currency-aware money labels on the Front-Office Overview (block 5). |
