# ADR-0113: Front-Office Charts — Unified Axis End, Plan-Tail Display, and Hero De-Clipping

- **Status:** Accepted (2026-08-05)
- **Date:** 2026-08-05
- **Deciders:** Soenke (ProcessReengineer)
- **Relates to:** ADR-0082 (per-archetype tile-sets), ADR-0093 (market-data
  tick), ADR-0103 §6 (system plan-NAV projection), ADR-0061 (benchmark
  import)
- **Roadmap:** (new item — number to be assigned in the roadmap chat)

## Context

The Charts section of the Front Office renders one article per active
investment with archetype-aware tiles (ADR-0082). Three interacting
properties make the surface misleading under stale pricing:

1. **The market-data tick refreshes only live-eligible investments**
   (ADR-0093; `services/investments/live_refresh.py`): listed types with
   a primary market identifier and a non-`'reported'` pricing mode.
   Private-markets investments are skipped by design, so their charts end
   at the last imported actual NAV and never move on a tick.

2. **No unified time axis exists.** Every Plotly figure auto-ranges its
   x-axis to its own data. Because every tile stretches its own date
   range across the same tile width, charts *look* parallel while
   covering different periods — an unmarked comparability illusion.

3. **The listed-archetype hero clips both cumulative lines to the
   investment ∩ benchmark monthly inner-join**
   (`services/analytics/benchmark_comparison.py`,
   `_align_monthly_series`, `join="inner"`). Benchmark observations are
   import-only — the tick never refreshes them — so a tick can extend
   the investment's monthly series and the inner-join cuts it straight
   back to the last common benchmark month. Even live-eligible heroes
   therefore appear frozen after a tick.

Meanwhile the data to show an honest continuation already exists:
`investment_navs` carries the `nav_kind` discriminator
(`'plan' | 'actual'`); plan rows are produced by the Excel import and by
the system cash-plan projection (ADR-0103 §6). Every Front-Office chart
currently consumes `'actual'` rows only.

## Decision

### 1. Unified right axis end ("universe as-of")

Every time-series tile in the Charts section receives the same x-axis
**end**: the latest `'actual'` NAV date observed across the tenant's
active investment universe. The route computes this date once per
section request and passes it into every spec builder.

The axis **start is not unified**. Vintages differ by years; a common
start would compress long-lived funds into a sliver. Each chart keeps
its own data-driven start.

### 2. Plan-tail display traces (NAV-space charts)

The NAV-space charts — the NAV-only archetype's full-width NAV
time-series and the Capital-Account archetype's NAV line inside the
Cashflows & NAV tile — gain one optional **plan-tail trace**:

- **Data:** `'plan'` NAV rows with
  `last_actual_date < as_of_date <= universe_as_of`, prefixed with the
  last actual point as an anchor so the tail visually joins the solid
  line.
- **Styling:** same hue as the actual trace but muted, dashed line,
  hover label suffixed `(Plan)`, own legend entry `Plan`.
- **Empty tail:** an investment without plan rows beyond its last actual
  simply shows its solid line ending before the unified axis end — a
  visible, honest gap. Nothing is fabricated to fill it.

### 3. Analytics remain actual-only

No `'plan'` row reaches any return, KPI, or statistics computation. The
plan tail is a **display-boundary concern**: it travels as separate DTO
fields from `ArchetypeChartsService` into the spec builders and is never
merged into the series that feed
`compute_cashflow_adjusted_return_series`, trailing returns,
volatility/Sharpe, TVPI/DPI/IRR, or any KPI pill. Actual and plan series
are never joined for calculation.

### 4. No zero-fill, no carry-forward in time-series charts

Gap months in a chart are **not** filled with 0 % returns or a
carried-forward NAV. A zero is a claim ("the value did not change"),
optically indistinguishable from a genuinely flat fund — unmarked
fiction, in conflict with the codebase-wide no-silent-fallback
principle. The carry-forward inside `compute_aum` (latest NAV at or
before the as-of date) is a stock-figure convention, remains as is, and
is out of scope here.

### 5. Hero de-clipping (listed archetypes)

The hero's two cumulative-return lines are decoupled from the benchmark
inner-join **at their ends only**:

- **Start:** aligned to the later of the two series' first months, so
  both cumulatives begin at 0 % on the same month and stay comparable.
- **Ends:** each line runs to its own last available month. A
  market-data tick that extends the investment's monthly series now
  visibly extends the investment line even while benchmark observations
  lag at the import state.
- **Excess area and metrics** (beta, tracking error, information ratio)
  continue to be computed on the inner-join intersection only — they are
  undefined outside it. The `n_observations` diagnostic and the
  empty-state behaviour ("No aligned observations") are unchanged.

### 6. Monthly-grid boundary behaviour (accepted property)

The unified axis end is a calendar date; the hero charts run on a
month-end grid. A hero therefore effectively ends at the last month-end
at or before the universe as-of. This is accepted, not worked around.

## Consequences

- A market-data tick produces visible movement: listed heroes extend to
  the newest investment month; the unified axis end advances for every
  tile; private-markets tiles show a dashed plan continuation where plan
  data exists, and an honest gap where it does not.
- Touch points: `ArchetypeChartsService` (load plan tails, new DTO
  fields, start-aligned full-length cumulatives in `_benchmark_block`),
  spec builders `investment_nav_timeseries`, `investment_cashflows_nav`,
  `benchmark_investment_total_return` (plan-tail trace / free line
  ends), a shared axis-end parameter across the tile spec builders, the
  charts route (compute universe as-of, thread it through), plus spec-
  and service-level tests.
- The Analytics purity contract is untouched: spec builders stay pure
  dict emitters; `services/analytics/` gains no plan awareness.

### Not in scope

- Return-space plan continuation on the heroes (Stage 2; requires its
  own modelling decisions — splice point, benchmark treatment inside the
  plan window, excess handling — and a successor ADR).
- Plan cashflow bars and projected TVPI/DPI in Capital-Account tiles.
- Live refresh of benchmark observations (separate finding, separate
  ticket).
- Portfolio-level aggregate charts and the Portfolio Review surface.

## Alternatives considered

- **Zero-fill missing months** — rejected: unmarked fiction; hides
  staleness instead of showing it (Decision 4).
- **Splicing plan values into the return series** — rejected:
  contaminates performance analytics with projections (Decision 3).
- **Status quo per-chart auto-range** — rejected: equal tile widths
  over unequal periods fake comparability.
- **Unifying the axis start as well** — rejected: vintage compression
  destroys readability of long-lived funds.
- **Axis end = today** — rejected: on non-trading days every chart
  carries an empty right margin with no information gain; the newest
  universe actual is the honest frontier.
