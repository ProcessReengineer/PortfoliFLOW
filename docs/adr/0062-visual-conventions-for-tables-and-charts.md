
# ADR-0062: Visual Conventions for Tables and Charts (Program-Wide)

- **Status:** Accepted (2026-05-26 — pilot-implemented in the Benchmarks & Attribution section as part of the same commit batch)
- **Date:** 2026-05-26
- **Deciders:** PortfoliFLOW project owner
- **Tags:** ui, theming, charts, tables, cross-surface, design-system, phase-1b

---

## Context

PortfoliFLOW has accumulated several surfaces with tabular and chart-based displays — Statistics, Investment Limits, Portfolio Review, and most recently Benchmarks & Attribution (ADR-0061). Each surface was implemented independently, and each invented its own visual decisions for sort behaviour, performance-column tinting, empty-state appearance, drill-down interaction, and annotation density.

The local consequences are tolerable in isolation, but the cross-surface effect is corrosive: a reader who has learned that "click a row to drill in" on Limits encounters a "select from the dropdown above the chart" on Benchmarks; a reader who has learned that "green means good" on Portfolio Review encounters identical green tints used decoratively on an unrelated control elsewhere. Reader expectations stop transferring between surfaces, and every new section becomes a re-learning exercise.

This becomes a real cost as the product matures toward institutional readers. Practitioners read these surfaces for decision support; they expect the visual grammar to be consistent enough that they can spend their attention on the data rather than on the controls. A program-wide convention is required before the surface count grows further.

The Phase-1b "Polish" round of A12 (Benchmarks & Attribution) is the natural moment to codify these conventions — the section is informationally complete but visually inert, and the polish work necessarily decides each of the five questions below. Deciding them program-wide at the same time costs little extra and pays back across every future surface that consumes the conventions.

---

## Decision

Codify five program-wide visual conventions. Each is descriptive of a class of UI decision; each can be migrated to existing surfaces independently in its own follow-up commit.

### 1. Tabular data with a sort dimension uses Tabulator

Any table where the operator might plausibly want to re-order rows — by ascending/descending value, by category, by name — must be rendered with [Tabulator](https://tabulator.info/), which is already loaded globally via `web/templates/base.html`. Custom `<table>` markup is forbidden for this class.

Pure read-only tables with a fixed semantically meaningful row order (e.g. a key-value attribute list, a configuration summary) may still use plain `<table>`. The distinguishing test: *if the table has more than three rows, would a competent reader reach for a "sort by this column" affordance?* If yes — Tabulator. If no — plain HTML is acceptable.

### 2. Performance columns receive heatmap-style background tinting

A **performance column** is any column whose values are operator-actionable signals of good vs. bad — e.g. Excess Return, Information Ratio, Alpha. Higher is unambiguously better (or lower is unambiguously better) and the magnitude carries decision-relevant meaning.

A **diagnostic column** is one that describes the *behaviour* of a series without indicating success or failure — e.g. Beta, R², Tracking Error, Up/Down Capture Ratio, observation count. These columns must **not** be tinted.

The tinting uses two new dark-theme-tuned semantic tokens:

- `--ui-semantic-table-pos-bg-dark` (desaturated dark green, low contrast against the canonical dark background)
- `--ui-semantic-table-neg-bg-dark` (desaturated dark red, same)

Intensity is proportional to magnitude, capped at a sensible bound. The standard bound per column type is:

- **Percentage columns** (Excess p.a., Alpha p.a.): cap at ±20pp
- **Ratio columns** (Information Ratio): cap at ±1.0

A simple two-bucket mapping (`pos-mild` / `pos-strong`, mirrored on the negative side, plus a `neutral` no-tint bucket) is sufficient and avoids the visual noise of finer gradations. Strong tint uses the token directly; mild tint uses `color-mix(in srgb, <token> 50%, transparent)`.

Implementation seam: emit a `data-magnitude-bucket="pos-strong" | "pos-mild" | "neg-strong" | "neg-mild" | "neutral"` attribute on the cell; the CSS picks up the attribute and applies the background.

The existing light-theme tokens `--ui-semantic-table-pos-bg` / `--ui-semantic-table-neg-bg` in `theme.css` are kept for a future multi-theme strategy but are not in active use; the `-dark` variants are the operative tokens until that decision is taken.

#### 2.1 Directionality and one-sided magnitude

The heatmap convention as originally stated assumes *higher is better*: positive magnitudes tint green, negative magnitudes tint red. Two extensions are needed when migrating the convention to surfaces whose domain semantics differ.

**Direction.** A heatmap column declares one of:

- `higher-better` — large positive values tint green, large negative values tint red. The Benchmarks Excess and IR columns use this.
- `lower-better` — large values tint red, small values are untinted (or tinted green if a `two-sided` magnitude is also declared; see below). Used for Investment Limits Coverage, where a high Coverage value means low headroom.

**Magnitude variant.** A heatmap column declares one of:

- `two-sided` — the value can be meaningfully positive or negative around a neutral zero. The Benchmarks Excess column uses this: a +5% excess tints mild-green, a −5% excess tints mild-red, near-zero is untinted.
- `one-sided` — the value is bounded on one side (typically `≥ 0`) and the gradient runs from neutral at the floor to strong-tint at the cap. The Limits Coverage column uses this: 0% is neutral, 100% is strong-red under `lower-better`.

The two parameters compose: `higher-better + two-sided` (Excess), `lower-better + one-sided` (Coverage). The remaining combinations (`higher-better + one-sided`, `lower-better + two-sided`) are not yet used in any surface; they are permitted but should be declared explicitly.

**Implementation seam.** Continue to emit `data-magnitude-bucket="pos-strong" | "pos-mild" | "neg-strong" | "neg-mild" | "neutral"` on the cell. The CSS rules remain unchanged: `pos-*` buckets paint the green tokens, `neg-*` buckets paint the red tokens. The *interpretation* of which value maps to which bucket is the producer's responsibility (the Tabulator formatter callback or the server-side projection helper). This keeps the CSS surface stable across direction/magnitude variants — only the bucket-assignment logic varies per column.

**Concretely, for `lower-better + one-sided` Coverage (cap at 1.0 = 100%):**

| Coverage range | Bucket |
|---|---|
| `>= 0.90` | `neg-strong` |
| `>= 0.70` | `neg-mild` |
| `< 0.70` | `neutral` |

The thresholds are aligned with the conventional WARN/BREACH boundaries used by the status badges, but they are *independent* of them: the badge encodes the regulatory threshold; the heatmap encodes the perceived risk gradient. A row at 89% Coverage shows a mild-red tint *and* a WARN badge — the two are complementary, not redundant.

#### 2.2 Per-column heatmap parameters and the column catalogue

When a single table carries many heatmap columns with heterogeneous direction / magnitude / cap, each column must declare its own parameters explicitly. The convention is:

A heatmap column declares a four-tuple:

```
{ direction: "higher-better" | "lower-better",
  magnitude: "two-sided" | "one-sided",
  cap: number,         // the magnitude at which the strong tint is reached
  pivot?: number }     // optional reference point; defaults to 0
```

The producer (the Tabulator formatter callback or a server-side projection helper) consumes the four-tuple, computes a normalised magnitude relative to `pivot` and `cap`, and emits the `data-magnitude-bucket` attribute. The CSS rules remain unchanged: `pos-strong` / `pos-mild` / `neg-strong` / `neg-mild` / `neutral`.

**Bucket assignment logic (canonical, reused across surfaces):**

```
Let v = cell value, p = pivot (default 0), c = cap, d = direction, m = magnitude.

If v is null or NaN:               bucket = "neutral"
If m == "one-sided" and d == "lower-better":
    ratio = (v - p) / c
    If ratio >= 0.7:               bucket = "neg-strong" if ratio >= 1.0 else "neg-mild"
    Else:                          bucket = "neutral"
If m == "one-sided" and d == "higher-better":
    ratio = (v - p) / c
    If ratio >= 0.7:               bucket = "pos-strong" if ratio >= 1.0 else "pos-mild"
    Else:                          bucket = "neutral"
If m == "two-sided" and d == "higher-better":
    ratio = (v - p) / c
    If ratio >=  0.5:              bucket = "pos-strong"
    Elif ratio >   0:              bucket = "pos-mild"
    Elif ratio <= -0.5:            bucket = "neg-strong"
    Elif ratio <   0:              bucket = "neg-mild"
    Else:                          bucket = "neutral"
If m == "two-sided" and d == "lower-better":
    same as higher-better, with signs flipped (multiply ratio by -1
    before bucketing).
```

**Statistics column catalogue (Statistics surface ADR-0062 pilot):**

| Table | Column | Direction | Magnitude | Cap | Pivot | Notes |
|---|---|---|---|---|---|---|
| Distribution | Mean (daily) | higher-better | two-sided | 0.0008 | 0 | ≈ ±20pp annualised at 252d. |
| Distribution | Mean (ann.) | higher-better | two-sided | 0.20 | 0 | ±20pp matches Benchmarks-Excess cap. |
| Distribution | Std Dev (daily) | lower-better | one-sided | 0.025 | 0 | ≈ 40% annualised at the cap. |
| Distribution | Std Dev (ann.) | lower-better | one-sided | 0.40 | 0 | Pension-fund / endowment tails out at 40%. |
| Distribution | Variance (daily) | lower-better | one-sided | 0.0006 | 0 | sqrt(0.0006) ≈ 0.025 daily std. |
| Distribution | Skewness | (diagnostic) | n/a | n/a | n/a | **No heatmap** — diagnostic only. |
| Distribution | Kurtosis (excess) | (diagnostic) | n/a | n/a | n/a | **No heatmap** — diagnostic only. |
| Distribution | Median | higher-better | two-sided | 0.0008 | 0 | Same as Mean (daily). |
| Distribution | Min Return | higher-better | two-sided | 0.05 | 0 | A −10% min reads strong-red; −1% reads mild-red. |
| Distribution | Max Return | higher-better | two-sided | 0.05 | 0 | Symmetric counterpart. |
| Risk | VaR 90 / 95 / 99 | higher-better | two-sided | 0.05 | 0 | Negative values; higher (closer to zero) is better. |
| Risk | CVaR 95 | higher-better | two-sided | 0.05 | 0 | Same convention as VaR. |
| Risk | Max Drawdown | higher-better | two-sided | 0.50 | 0 | Decimal form; −50% is strong-red. |
| Risk | Ulcer Index | lower-better | one-sided | 15.0 | 0 | Unbounded above; capped at 15 for visual purposes. |
| Risk | Downside Deviation | lower-better | one-sided | 0.02 | 0 | Daily, non-annualised. |
| Risk/Return | Sharpe Ratio | higher-better | two-sided | 2.0 | 0 | Asset-mgr universe spans roughly ±2. |
| Risk/Return | Sortino Ratio | higher-better | two-sided | 3.0 | 0 | Wider band than Sharpe. |
| Autocorrelation | Lag 1 / 2 / 3 / 4 | (diagnostic) | n/a | n/a | n/a | **No heatmap** — diagnostic only. |

The caps in the catalogue are calibrated to typical institutional ranges for liquid funds and standard PE/RE; they may need adjustment after a visual review against the seeded test data. Surface the call if the rendered tinting under-fires (most cells neutral) or over-fires (most cells strong) in a way that suggests the cap is wrong.

### 3. Empty tiles in small-multiples grids are visually dimmed

A small-multiples grid (e.g. asset-class composites vs. benchmarks) commonly contains tiles for categories that exist in the catalogue but have no own-data attached (e.g. an asset class with a mapped benchmark but no funded investment). These empty tiles must be visually dimmed so the eye is drawn first to the populated tiles.

Concrete rules:

- Any benchmark line drawn for context in an empty tile uses its base colour at **alpha 0.45** rather than full opacity. The benchmark line is *not* suppressed — it provides market context — but it must clearly read as "no own data here".
- The subplot title for an empty tile uses the secondary text colour at alpha 0.55.
- Populated tiles retain full opacity for both line and title.

The convention applies to small-multiples grids regardless of subject (benchmarks, limits coverage, future treemap small-multiples). The dimming alpha values are project-tuned and should remain in lock-step with `0.45 / 0.55` until or unless a contrast review re-evaluates them.

### 4. Master-detail tabular drill-down expands inline

When the operator wants to see detail for one row in a table, the detail panel opens **inline as a sibling row directly beneath the master row**, not as a separate pane below the table and not via a dropdown-then-load-pane pattern.

Concrete rules:

- Clicking a row toggles an expansion that is visually attached to the parent row.
- Only one row may be expanded at a time. Clicking a second row collapses the first.
- Re-clicking the expanded row collapses it.
- The expansion content is fetched lazily via `fetch()`; the row-expand is a UI affordance, not a routing event.
- The cursor on clickable rows is `pointer`.

Tabulator's standard `rowClick` + `rowFormatter` API is the canonical implementation seam. Surfaces that already use plain `<table>` and need this pattern migrate to Tabulator first (per Convention 1).

### 5. Empty-state messages use `.pf-empty-state` as the canonical class

The class `.pf-empty-state` is reserved program-wide for short prose messages that occupy the space where data would otherwise be. Examples: "No investments yet — import via the Data Import section", "Select an SAA configuration to see the comparison".

Concrete rules:

- The class is defined exactly once, in a canonical sheet (currently `web/static/css/components/empty_states.css`, registered in `base.html`).
- Surface-specific stylesheets must not redefine it.
- Visual style: secondary text colour, italic, `padding: 16px 0; margin: 0;`. Nothing more elaborate — empty states should disappear when data arrives, not compete for attention while they are visible.

Prior to this ADR several templates referenced `.pf-empty-state` without a global definition; the class was being styled by accident through surrounding rules. The canonical definition is added as part of the same commit batch as this ADR.

### 6. Chart annotations carry the headline finding

A chart whose headline finding can be stated in a single sentence must carry that sentence as an in-chart annotation, positioned so the reader sees it before reading the axes. The annotation belongs to the chart, not to the surrounding prose, so the chart remains self-explanatory when embedded in a report, a printed handout, or any other context outside its origin section.

Positioning:

- For wide chart formats (single-chart blocks at full content width) the annotation sits top-right of the plot area, single-line where possible.
- For small-multiples grids the annotation sits inside each tile (per-tile badges, as pilot-implemented in Stage b of Benchmarks & Attribution).
- For narrow viewports where in-chart placement would crowd the axes, the annotation may be rendered as a caption block immediately above or below the chart, but it must still be styled as belonging to the chart (small font, secondary text colour, no extra container chrome).

The annotation is *terse* — three short clauses at most. Long-form interpretation belongs in the surrounding prose, not in the chart annotation.

The pilot implementation is the Stage-c SAA-hypothetical chart in the Benchmarks & Attribution section, which states the three cumulative endpoints (Actual, SAA × Benchmark) and the allocation effect in percentage points as a single line top-right of the plot area, with the allocation-effect clause colour-coded by sign.

---

## Rationale

**Why a single program-wide ADR rather than per-surface decisions?**
Each of the five conventions answers a question that *every* tabular/chart surface eventually has to answer. Deciding them once, with a worked pilot implementation, costs roughly the same as deciding them on the third surface in a row — and saves the cost of re-deciding them on the fourth, fifth, and sixth. The conventions are intentionally minimal (five rules, each one paragraph) precisely so they can be applied by inspection rather than by consulting a 20-page style guide.

**Why Tabulator specifically?**
Tabulator is already loaded globally; the marginal cost of using it is near zero. Alternative client-side table libraries (DataTables, AG Grid, TanStack Table) would each add a dependency without solving a meaningfully different problem at the project's current scale. The decision is "use what's there", not "Tabulator is uniquely correct" — if a future need outgrows Tabulator the choice can be revisited as a separate ADR.

**Why magnitude-proportional shading, not per-cell semantic classes?**
A binary `class="cell-positive"` / `class="cell-negative"` scheme would mark direction without magnitude — a 0.1% excess and a 30% excess would look identical. The magnitude signal is the decision-relevant content; preserving it via two intensity buckets per direction keeps the legend obvious (full tint = "this is large", half tint = "this is mild") while avoiding the visual noise of a continuous gradient.

**Why alpha 0.45 for empty-tile dimming, not 0.30 or 0.60?**
0.45 is the project-tuned value that makes empty tiles visibly secondary at small-multiples grid scale (3–4 columns at typical screen widths) without causing the benchmark line to disappear. It is intentionally not a round number — it is a calibration result, not a derived constant. If a future contrast audit re-tunes it, every surface should move together.

**Why inline master-detail expand instead of a dedicated detail pane?**
A dedicated pane below or beside the table introduces a "where did the focus go?" moment for the operator and consumes screen real estate even when no row is selected. Inline expansion is spatially local — the detail is attached to the master row — and is dismissable by the same click that opened it. Tabulator's row-formatter API supports the pattern natively, which keeps the implementation lightweight.

---

## Alternatives Considered

- **Per-surface convention (status quo).** Each surface invents its own conventions. Rejected — produces no transfer of reader expectation across surfaces; new surfaces incur a re-learning cost and existing surfaces drift.

- **A heavy design-system library (shadcn/ui, Material UI, Carbon).** Adopt a full external design system and re-skin all surfaces to match. Rejected as a dependency-weight escalation without proportional benefit at the project's current scope. The five conventions captured here cover the practical surface area without the maintenance cost of tracking an external library's release cadence.

- **Per-cell semantic colour classes** (`class="cell-positive"` / `class="cell-negative"`) instead of magnitude-proportional shading. Rejected — loses the magnitude signal, which is the actionable part of the visual.

- **A unified KPI-card component as the only acceptable empty-state element.** Replace `.pf-empty-state` prose with a card carrying an icon and primary action. Rejected for Phase 1b — the prose form is sufficient for the current surfaces, and the card form can be added later as an opt-in upgrade where a primary action exists.

- **Pre-computed pre-mixed hex tokens** for the `pos-mild` / `neg-mild` intensities, rather than using CSS `color-mix`. Rejected for the active surfaces — `color-mix` is supported in all modern browsers (Chrome 111+, Firefox 113+, Safari 16.2+) and the project's existing CSS already uses modern features. If legacy-browser support ever becomes a requirement, the fallback is to add the pre-mixed hex tokens to `ui_theme.json`.

---

## Consequences

### Positive

- A reader who learns one surface transfers expectations to the next. Cross-surface visual grammar becomes a small but real readability lever.
- New ADRs for surface-specific polish can reference `§<convention>` of this ADR instead of re-deriving each decision.
- The pilot implementation on the Benchmarks & Attribution section provides a concrete worked example that future migrations can copy.
- The implementation seams (attribute selectors for heatmap tinting, Tabulator's row-formatter API for master-detail) are simple enough that surface migrations can be done in a single focused commit per surface.

### Negative

- Existing surfaces (Statistics, Investment Limits, Portfolio Review) become temporarily inconsistent with this ADR until follow-up prompts migrate them. Each surface's migration is its own atomic commit; the timeline is on the roadmap.
- Two new semantic tokens add to the `ui_theme.json` surface area. The cost is small but cumulative — token sprawl is a real long-term concern and the project should be deliberate about adding more semantic tokens versus reusing existing ones.

### Neutral / Follow-ups

- Per-surface migration prompts (one each for Statistics, Investment Limits, Portfolio Review) are anticipated as the natural next polish-round items.
- A potential future convention — "all charts share the same legend placement and time-axis tick density" — is deferred until enough surfaces have visible legends to compare. It is not a Phase-1b decision.
- A potential future convention — "KPI strips use a 4-card grid above the primary table" — is anchored by the Benchmarks & Attribution pilot but not yet codified at program level; one data point is not enough.

---

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Usability (Learnability and Operability improve via cross-surface convention transfer; the reader's mental model is reusable), Maintainability (visual changes localise to canonical tokens and class definitions; surface-specific stylesheets shrink), Functional Suitability (the decision-relevant magnitude signal is preserved in performance columns via proportional shading rather than lost to a binary classification).
- **Audit evidence:** This ADR, the pilot implementation on the Benchmarks & Attribution section (commit batch dated 2026-05-26), the new tokens in `config/ui_theme.json` and their regenerated counterparts in `web/static/css/theme.css`, and the canonical `.pf-empty-state` definition in `web/static/css/components/empty_states.css`.

## References

- ADR-0021 (Theming foundation)
- ADR-0025 (Theme token canonicalisation)
- ADR-0045 (Charts/Statistics web migration and analytics service foundation; chart-spec convention)
- ADR-0046 (Region model — establishes hard-fail-on-unknown-label discipline cited as a precedent for visual-rule discipline)
- ADR-0061 (Benchmarks & Attribution — Schema, Import, and Analytics; the section that pilot-implements this ADR)
- Roadmap A12 Phase 1b (Benchmarks & Attribution Quick-Wins polish — the change set that carries this ADR)

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-05-26 | Project owner + assistant | Initial decision. Five program-wide visual conventions codified. Pilot-implemented in the Benchmarks & Attribution section as part of the same commit batch. |
| 2026-05-26 | Project owner + assistant | Added §6 (chart annotations carry the headline finding) as part of the Stage-c polish round; pilot-implemented in `services/chart_specs/benchmark_saa_hypothetical.py`. |
| 2026-05-28 | Project owner + assistant | Added §2.1 (directionality and one-sided magnitude) as part of the Limits migration. Pilot-implemented in the Investment Limits Coverage column with `lower-better + one-sided`. |
| 2026-05-29 | Project owner + assistant | Added §2.2 (per-column heatmap parameters and the Statistics column catalogue) as part of the Statistics migration. Establishes the four-tuple convention for tables with heterogeneous heatmap columns and pilot-applies it to the four Statistics tables. |
| 2026-06-03 | Project owner + assistant | The four Statistics detail tables (Distribution, Risk, Risk / Return, Autocorrelation) are now presented as native `<details>` collapsibles (`stats-subblock`), collapsed by default, to reduce the section's vertical footprint. This is distinct from the removed legacy per-investment `stats-details` collapsibles (a different structure and class). Because each table uses `layout:"fitColumns"` and a Tabulator initialised inside a closed disclosure container measures a zero-width box, the table is registered and `redraw(true)` is called on the sub-block's `toggle`-open event so columns size correctly on first reveal. |
