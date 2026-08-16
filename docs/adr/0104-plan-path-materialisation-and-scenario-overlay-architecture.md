# ADR-0104: Plan-Path Materialisation and Scenario Overlay Architecture — the Planning Desk

- **Status:** Accepted
- **Date:** 2026-07-13
- **Deciders:** PortfoliFLOW project owner
- **Tags:** planning-desk, scenario, overlay, plan-path, fx, htmx, module-registry, engine-contract, phase-8
- **Depends on:** ADR-0103 (cash as first-class asset class; the cash plan path), ADR-0097/0098 (position model, materialisation), ADR-0099 (FX conversion seam), ADR-0060 (plan/actual cut-over), ADR-0066 (cashflow-adjusted returns), ADR-0013/0045 (analytics purity), ADR-0058 (module registry)
- **Amends:** ADR-0089 (Decision Console v0 — the §Scenarios placeholder anchor for #034 relocates; see §8)
- **Honours:** `docs/handover/compatibility-annex-adr-a-adr-c.md` §B and §C
- **Interaction basis:** `docs/handover/planning-desk-interaction-decisions.md` and `docs/handover/planning-desk-mockup-v2.html` (both 2026-07-13)
- **Commissions (does not write):** ADR-D (Takahashi-Alexander engine, #023), ADR-E (scenario regimes, #034 richer timing)

---

## Context

With the position model (ADR-0097/0098) and cash as a first-class
asset class (ADR-0103), the book carries everything a forward-looking
surface needs: actual NAV series, plan NAV series (value-based,
ADR-0060), plan flows, the materialised cash plan path, and one FX
conversion seam. What does not exist is (a) a place for a human to
*work* with the future — stretch a drawdown, insert a hypothetical
trade, apply a shock — and (b) a contract that lets such work happen
without ever touching the book or forking an engine.

The concept decisions of 2026-07-07 (D15–D21), reconfirmed 2026-07-13,
pin the shape: a scenario is a **parameter set, not a dataset**; the
baseline is the existing plan world; transformations are ordered and
pure; engines consume transformed frames unchanged. Multi-currency
added new territory, decided 2026-07-13: plan-world FX = carry-forward
held flat (N1), projection per currency (N2), FX shocks as a
**fourth transformation kind** acting on the conversion seam (N3).
The interaction model was fixed against mockup v2 before this text,
per the mock-before-implement rule.

## Decision

### 1. Two layers: persisted baselines, ephemeral overlays

- **Layer 1 — the book (persisted):** actual NAV series (imported,
  materialised per ADR-0098), plan NAV series (imported, value-based),
  plan flows, investor flows, and the materialised cash plan path
  (ADR-0103 §6). Nothing in this ADR writes to Layer 1.
- **Layer 2 — the overlay (ephemeral):** an ordered list of pure
  transformations applied to data frames *assembled from* Layer 1,
  recomputed per request, held nowhere. Scenario persistence, if it
  ever ships, persists **parameter sets** — never transformed data.

**The scenario baseline (D19)** is the plan world as it stands:
`nav_kind='plan'` series and plan flows per investment, the ADR-0103
cash plan path, with ADR-0060 carry-forward as the fallback where a
plan stream is missing. Plan rows stay value-based (annex §B.1);
drift-based projection is a possible later, explicitly labelled
alternative baseline — not v1.

### 2. The overlay contract — four transformation kinds

An overlay is `[transformation, …]`, applied in list order to the
assembled baseline frames. Exactly four kinds exist:

| kind | scope | acts on | executor dispatch |
|---|---|---|---|
| `insert_transaction` | one investment | holdings/value path + settling cash path | `resolve_archetype` |
| `repace_flows` | one capital-account investment | remaining plan-flow profile | `resolve_archetype` |
| `market_shock` | one archetype | value paths (price path for unitised, NAV path for reported) | `resolve_archetype` |
| `fx_shock` | one currency | the **conversion seam** — the plan-world FX path for that currency | none (archetype-blind) |

- **Purity:** every executor is a pure function `frames → frames`
  living in the analytics-adjacent overlay module (DB-free,
  FastAPI-free, Qt-free — the ADR-0013 regression guard extends to
  it). Executors never import repositories or sessions.
- **`insert_transaction`** (hypothetical): carries the field shape of
  the actual-entry form (type, investment, trade date, units, price,
  consideration). It adjusts the investment's hypothetical value path
  and debits/credits the cash path of its settlement currency
  (settle-against-cash). It **never** writes `position_transactions`,
  `investment_navs`, or `instrument_prices` (annex §B.2); hypothetical
  value paths may reuse the pure `holdings × price` computation of
  `services/investments/holdings.py`, but the ADR-0098 book
  materialisation is never called with, or extended by, scenario
  branches (annex §B.4). AUM invariance — `Σ NAV_plan + cash_plan`
  unchanged by any insert — is a construction property; the UI renders
  it as an inert badge, and a regression test asserts it over the
  executor.
- **`repace_flows`** (D18, Variant B): time-scales the **remaining**
  manager-plan drawdown profile by a factor in [0.5, 2.0];
  mid-position (1.0) reproduces the plan exactly — bit-identical
  frames, regression-tested. Funds without a manager plan are not
  re-paceable until the TA engine (#023, ADR-D) supplies a generated
  profile; the surface shows the row disabled rather than hiding it.
  TA is the *generator* for missing plans and the engine for richer
  #034 regimes — never calibrated to reproduce existing plans.
- **`market_shock`:** per-archetype operators (v1: price-level /
  NAV-level shift, magnitude in %), timing v1 = immediate at t₀.
  Richer timing regimes (paths, lagged, mean-reverting) are ADR-E
  territory and out of scope here. Scenario price paths live only in
  overlay structures, never in `instrument_prices` (annex §B.3).
- **`fx_shock`** (N3, decided 2026-07-13): scoped to a currency,
  restating the plan-world FX path (§3) used to translate **every**
  position of that currency — NAVs, plan flows, cash paths — into the
  functional currency. It executes at the conversion seam, *after*
  value-level transformations and *before* functional-currency
  aggregation. It is deliberately a separate kind, not a scope variant
  of `market_shock`: the intervention point (values vs. seam) is the
  essential difference between transformations, and hiding it behind a
  scope discriminant would embed a concealed branch in one executor.
  The UI form is nonetheless unified across both shock kinds
  (Scope → Operator → Magnitude → Timing).
- **Exemption invariant (binding, ADR-0103 §5):** no transformation
  of any kind creates, deletes, re-paces, or re-scales an
  `investor_flow`. Enforced inside every executor and asserted by a
  regression test at the overlay level.

### 3. Plan-world FX (N1/N2)

- **Convention (N1):** future plan values convert at the
  `FxConverter` carry-forward idiom extended past the last actual
  rate — the latest available rate, held flat over the plan horizon.
  Pinned explicitly; no silent alternative, no drift, no forward
  curve in v1.
- **Per-currency projection (N2):** cash paths and plan values are
  assembled in position currency and converted at the ordinary
  ADR-0099 §4 seam. FX-rate completeness is therefore a hard
  prerequisite of the plan world on multi-currency tenants:
  `MissingFxRateError` propagates typed (no 1:1 fallback), and the
  Planning Desk surfaces it as an actionable error naming the missing
  pair — the Irene-beat lesson applied at design time.
- An active `fx_shock` restates the held-flat path for its currency
  before the seam conversion runs.

### 4. Parameter-set serialisation and HTMX round-trip

- The parameter set is the **entire page state**: an ordered list of
  the §2 transformations, serialised as flat request parameters on
  every HTMX interaction (exact encoding fixed in the implementation
  strand; deterministic field order; no LLM-formed keys).
- The server holds **no scenario state** — every render recomputes
  from (book, parameters). The Baseline/Scenario toggle renders the
  same regions with an empty transformation list.
- "Copy scenario link" serialises the set into the URL — the only
  persistence affordance in v1. Later persistence = a table of named
  parameter sets, nothing else; explicitly out of scope now.
- The sticky parameter strip renders one removable chip per
  transformation; "Reset all to plan" empties the set. Pacing
  mid-position emits no chip (it *is* the plan).

### 5. Result assembly — engines as pure consumers (D17)

- All scenario results come from the **existing** engines — coverage
  (`limit_coverage`), composition, cashflow-adjusted return
  (ADR-0066) — fed with transformed frames at their normal assembly
  seams. No engine forks, no scenario branch inside
  `services/analytics/`.
- **Binding presentation rules** (from mockup v2, architectural
  because they are correctness rules, not styling):
  - **Shared-axis rule:** the baseline and scenario projected-path
    panels use identical axis scales, determined jointly from both
    worlds' extrema. Independent auto-scaling is prohibited — it
    visually understates scenario impact.
  - **Identical-history invariant:** left of t₀ both worlds are the
    same path by definition (overlays never touch actuals). This is a
    regression-testable invariant of the overlay pipeline, not just a
    display fact.
  - **Deltas-first:** every KPI renders as the pair (baseline,
    scenario, delta); the scenario panel repeats the baseline as a
    ghost line.
- v1 result regions: projected Σ-NAV and cumulative cashflow-adjusted
  total-return pair, KPI strip (AUM, tightest AnlV headroom,
  functional cash at t₀+4Q, plan-horizon breaches), coverage headroom
  per limit family, composition drill-down as a lazy partial.

### 6. The Planning Desk — seventh area

- The module registry (ADR-0058) gains the area `planning_desk`
  (display name **Planning Desk**, final per D20), with two stacked
  sections following the existing `pf-area` idiom (no tabs):
  `cash-flow-planning` and `scenario-analysis`, plus the sticky
  parameter strip above both. One workspace, one parameter set, two
  lenses.
- Timeline defaults: quarterly periodisation (monthly toggle),
  8-quarter horizon (4Q/12Q options), per-currency **balance** rows
  with a functional-currency total, the ADR-0060 seam as a single
  amber rule.
- The Planning Desk is a tool for active human use. Shirley/Irene
  access to its functions is out of scope and undecided.

### 7. Delimitation of transaction entry (D21)

Actual transactions continue to be entered where ADR-0098 §3 put
them — the positions surface on the investment detail page. The
Planning Desk holds **hypothetical** transactions only (§2). No entry
affordance for actuals appears on the Planning Desk, and no
hypothetical affordance appears on the detail page.

### 8. The Decision Console scenarios stub retires (D21 remainder)

`modules/decision_console/scenarios.py` and
`web/templates/_partials/decision_console_scenarios.html` — the
ADR-0089 placeholder anchor for #034 — are **deleted** in the
Planning Desk implementation strand, and the registry entry with
them. Feature #034 re-anchors on the Planning Desk's
`scenario-analysis` section in the roadmap. This amends ADR-0089's
location decision only; the Decision Console's identity sharpens to
what *has* changed (past/present), the Planning Desk owns what
*could* (future). Rationale for deletion over repointing: a panel
whose only content is a link is standing navigation debt, and a
possible future Irene-facing scenario view would build on this ADR's
overlay contract, not on a descriptive placeholder — the stub has no
reuse value. (Operator decision, 2026-07-13.)

### 9. Out of scope

- ADR-D (TA engine internals, profile generation, #023) and ADR-E
  (scenario timing regimes, #034) — commissioned by their strands.
- Scenario persistence beyond the parameter-set principle (§4).
- Shirley/Irene access to Planning Desk functions.
- Live FX supply (#042); FX hedging; secondaries on capital accounts;
  short positions; drift-based alternative baselines.
- The #046/#047 Portfolio-Review programme (separate track).

## Rationale

- **Parameter sets keep the system honest.** Statelessness is not an
  HTMX convenience — it is what makes "the book is never written from
  this page" checkable. If nothing persists, nothing can leak into
  the book; annex §B.2/§B.3 become properties of the architecture
  rather than disciplines of the implementer.
- **Four kinds, because intervention points differ.** Three
  transformations act on values and dispatch on archetype; the FX
  shock acts on the seam and is archetype-blind. A scope-union inside
  one shock kind would serialise almost identically but would make
  D16's dispatch statement false for one scope type and hide the
  seam/value distinction inside an executor branch — the same species
  of concealed compromise R1-a removed from the cash model.
- **Engines as pure consumers is the cheapest correctness proof.**
  Every scenario figure is produced by code that already has tests
  and production history; the overlay only changes *inputs*. The
  identical-history invariant and the pacing mid-position
  bit-identity give the pipeline two ends-of-the-rope regression
  anchors.
- **Deleting the stub follows the programme's own rule.** No
  structures held for undecided futures; the registry stays an
  inventory of what exists.

## Alternatives Considered

- **Tabs or a mode switch between the two sections:** Rejected — the
  sections are lenses over one parameter set; tabs would suggest two
  states where there is one, and the existing stacked-section idiom
  costs nothing.
- **FX shock as a scope parameter of `market_shock`:** Rejected
  (N3, 2026-07-13) — see Rationale; considered seriously because the
  serialisation and UI are near-isomorphic either way.
- **Server-side scenario state (session or table) for HTMX
  convenience:** Rejected — reintroduces the dataset model D15
  rejected, and every persistence bug becomes a potential book-purity
  bug.
- **Materialising scenario results for responsiveness:** Rejected for
  v1 — premature; the engines are fast at current book sizes, and
  caching ephemeral results would blur Layer 1/Layer 2.
- **Repointing or keeping the Decision Console stub:** Rejected —
  §8.
- **Writing ADR-D/ADR-E now:** Rejected — one concern per ADR; the
  overlay contract is deliberately closed under four kinds so both
  successors extend value sets additively (annex §C idiom) rather
  than reshaping this contract.

## Consequences

### Positive

- The future becomes explorable with zero book risk: every scenario
  figure is reproducible from (book, URL).
- The overlay contract is closed, small, and pure — four kinds, four
  executors, one exemption invariant, three regression anchors
  (identical history, pacing mid-position, AUM invariance).
- #034 gains a real architectural home; the Decision Console loses a
  dead panel and gains a crisper identity.
- ADR-D and ADR-E have a fixed contract to extend instead of a
  green field.

### Negative / cost

- Recompute-per-request puts engine latency on the interaction path;
  acceptable at current book sizes, and the first place to revisit if
  Planning Desk interactions ever feel slow (parameter-set
  memoisation would be the debt-free lever).
- FX-rate completeness becomes user-visible on multi-currency
  tenants: a missing pair blocks the plan world loudly. This is the
  designed behaviour, but it raises the operational bar for workbook
  FX-sheet coverage until #042 lands.
- The purity guard's scope grows (overlay module joins the
  regression-tested surface).

### Operator action required

- Accept this ADR together with ADR-0103; register both in
  `docs/adr/README.md` and commit before Phase 4 produces any
  implementation prompt.
- Confirm plan-data reality on the v31 database (which investments
  carry `nav_kind='plan'` rows) before the Planning Desk UI strand —
  the D19 baseline's fixture coverage depends on it.

---

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-07-13 | PortfoliFLOW project owner | Proposed. The Planning Desk as the seventh Area: persisted baselines vs. ephemeral overlays, a scenario as a parameter set rather than a dataset, four ordered transformation kinds, engines consuming transformed frames unchanged. Amends ADR-0089 (the Decision Console §Scenarios placeholder anchor relocates). Commissions ADR-D (#023) and ADR-E (#034). |
| 2026-07-13 | PortfoliFLOW project owner | Accepted together with ADR-0103 (the §Operator action requirement — both accepted and registered before any implementation prompt). Registered in `docs/adr/README.md`; implementation tracked as roadmap **#049**, blocked on **#048**. No code has shipped against it yet. |
