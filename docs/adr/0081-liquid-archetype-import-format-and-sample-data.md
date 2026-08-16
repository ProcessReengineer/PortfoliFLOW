# ADR-0081: Liquid-Archetype Import-Format Extension and Sample-Data Coverage

- **Status:** Accepted — 2026-06-16
- **Date:** 2026-06-16
- **Deciders:** PortfoliFLOW project owner
- **Implements:** the Excel-import and sample-data layer of ADR-0079 (liquid-asset
  archetypes); deferred fidelity refinements tracked as roadmap **#031**
- **Tags:** schema, excel-import, liquid-archetypes, fixed-income, sample-data, data-import

---

## Context

ADR-0079 decided the per-investment presentation and analytics model for the two
mark-to-market archetypes (Total-Return Equity, Fixed-Income) and the reference-data
schema they need. ADR-0080 then historised the three composition-weight tables onto
the same time-series, `basis`-tagged pattern. Both decisions are merged and their
schema is fixed; **this ADR does not re-decide any of it**.

ADR-0081 is to the liquid archetypes what **ADR-0061** was to Benchmarks &
Attribution: the decision record that adds a new data domain to the Excel import
format and the importer, plus the sample-workbook coverage that lets the demo tenant
actually render the new surfaces. Without it the liquid-archetype tables exist but
hold no rows, so every Total-Return-Equity and Fixed-Income surface renders empty.

### What ADR-0079 / ADR-0080 already merged

The per-investment **data layer** is in place (migration `b016_add_liquid_archetypes`,
ADR-0079 §2; migration `b017`, ADR-0080). For self-readability, the relevant natural
keys and taxonomies are recapped (not redefined) here:

- `investment_bond_analytics` — natural key `(investment_id, as_of_date)`; `ytm` and
  `eff_duration` NOT NULL; `oas` and `convexity` nullable (not every manager reports
  all four); `basis TEXT NOT NULL CHECK (basis IN ('reported','computed'))`; standard
  `tenant_id` and audit columns. **No `tr_index`** — total return is derived on read
  (ADR-0079 §3, ADR-0013).
- `investment_rating_weight` — natural key `(investment_id, as_of_date, rating_bucket)`;
  `rating_bucket IN ('AAA','AA','A','BBB','BB','B','CCC_and_below','NR')`;
  `weight_pct NUMERIC CHECK (weight_pct >= 0 AND weight_pct <= 100)`, weights need not
  sum to 100; `basis` NOT NULL; `tenant_id`; audit.
- `investment_maturity_weight` — natural key `(investment_id, as_of_date,
  maturity_bucket)`; `maturity_bucket IN ('0-1y','1-3y','3-5y','5-7y','7-10y','10y+')`;
  `weight_pct`, `basis` NOT NULL, `tenant_id`, audit.
- `investment_navs.basis TEXT NULL` — additive; **NULL ⇒ treated as `reported`** (the
  NAV table keeps a nullable `basis`, unlike the three reference tables which are NOT
  NULL).

Adjacent facts the importer relies on but does not change: `flow_type` on
`investment_cashflows` already carries `dividend` and `coupon` with the signed-`amount`
convention (ADR-0043 §1); and `compute_cashflow_adjusted_return_series(nav, flows)`
(ADR-0066) is the single reused total-return primitive for both listed archetypes
(ADR-0079 §3).

### What is still missing

Three things stand between the merged schema and a tenant that renders the archetypes:

1. **The import format has no carrier** for bond analytics, rating/maturity composition,
   or listed-instrument income. The workbook has NAV and the private-markets cash-flow
   sheets, but nothing that maps onto `investment_bond_analytics`,
   `investment_rating_weight`, `investment_maturity_weight`, or onto `dividend`/`coupon`
   cashflows for listed funds.
2. **The importer does not parse or persist** any of those rows; the
   `transform_upload_to_investments(...)` path has no seam for the three reference repos.
3. **The sample workbook ships no liquid-archetype data** — and worse, the listed-fund
   rows it *does* carry are silently dropped on import because their row-2 type labels
   (`Credit`, `Cash`) resolve to no canonical `investment_type`.

This ADR settles the format extension, the importer wiring, the alias fix, and the
sample-data conventions. The synthetic generator and the v26 workbook themselves are
built in a **separate later prompt** and are out of scope here (ADR discipline: decide
before implementing).

---

## Decision

Four coordinated, additive changes. None of them touches the merged ADR-0079/0080
schema, and none touches the existing 15 workbook sheets or their parsing.

### 1. Four new sheet kinds in the Excel import format (additive)

The workbook gains four new sheet kinds. The income kind ships as an `actual`/`plan`
pair — mirroring how the existing cash-flow sheets already come paired — so the
workbook grows from 15 to 20 physical sheets; the three reference kinds have no
plan/actual axis and add one sheet each.

- **`Cash Flow Income actual`** and **`Cash Flow Income plan`** — the same **wide**
  idiom as the four existing Cash-Flow sheets: three header rows (names / type /
  sub-class), date-indexed from row 4, one column per investment name. The importer
  derives `flow_type` from the **resolved investment type**: `listed_equity` →
  `dividend`, `listed_bonds` → `coupon`. A single sheet pair carries both income kinds;
  there are **no separate dividend and coupon sheets**.

- **`Bond Analytics`** — **tidy/long**: one row per `(as_of_date, investment)` with
  columns `ytm | eff_duration | oas | convexity`. Maps 1:1 onto the
  `investment_bond_analytics` natural key `(investment_id, as_of_date)`. `oas` and
  `convexity` may be blank (the nullable columns).

- **`Rating Weights`** — **tidy/long**: `as_of_date | investment | rating_bucket |
  weight_pct`. Maps onto `investment_rating_weight`.

- **`Maturity Weights`** — **tidy/long**: `as_of_date | investment | maturity_bucket |
  weight_pct`. Maps onto `investment_maturity_weight`.

The long format is chosen for the three reference domains because each carries a third
dimension — the metric column for analytics, the bucket column for the weight ladders —
that the wide date×investment idiom cannot express cleanly. The established precedent
for a non-wide sheet is the `Benchmark Mapping` sheet (ADR-0061). Income flows keep the
wide cash-flow idiom because they are a plain date×investment quantity.

### 2. Importer extension (mirroring the ADR-0061 benchmark wiring)

- **`modules/front_office/data_import.py`** gains new sheet-category constant(s). The
  two income sheets **join the investment-time-series category** (they are parsed like
  the existing cash-flow sheets); the three reference sheets each get their own
  **dedicated parser**, following the `_parse_benchmark_mapping_sheet` pattern.
  `_sheet_name_to_key` is **unchanged** — it computes keys and needs no table edit.

- **`services/data_normalization/investment_extractor.py`** gains
  `extract_bond_analytics`, `extract_rating_weights`, and `extract_maturity_weights`,
  plus an income-flow path that emits `ImportedCashflow` rows with `flow_type ∈
  {dividend, coupon}` resolved from the investment type. All failures are collected as
  row-level `ImportRowError` (partial-success convention, ADR-0043 §3) — never thrown.
  The extractor stays DB-free; FK resolution from names to UUIDs happens at the service
  layer, exactly as for the existing extractors.

- **`InvestmentService.transform_upload_to_investments(...)`** is wired via **three new
  optional repository parameters** — `investment_bond_analytics_repository`,
  `investment_rating_weights_repository`, `investment_maturity_weights_repository` —
  opt-in in exactly the same way as the existing `region_weights_repository` /
  `sector_weights_repository`. Income flows take the **existing cashflow write path**;
  no new repository is needed for them. The web-route call sites in
  `web/routes/data_import.py` (`_run_dry_run_extraction` and the commit path) pass the
  three new repositories.

- **Persistence semantics.** The three reference tables use a single-row **`upsert`** on
  their natural key, so re-importing the same workbook is idempotent; income uses the
  cashflow path. **Every new row is tagged `basis="reported"`.** NAV `basis` stays NULL
  (⇒ reported) — no change to the NAV write path.

### 3. Investment-type alias fix — `Credit` and `Cash`

Add two entries to `_INVESTMENT_TYPE_ALIASES`
(`services/data_normalization/investment_extractor.py`):

- `"credit" → "listed_bonds"`
- `"cash"   → "listed_bonds"`

Today the sample workbook's row-2 labels `"Credit"` (Investments K/L/M) and `"Cash"`
(Investment T) resolve to nothing, and those rows are **silently dropped on import** —
yet the credit funds and the money-market fund are precisely the Fixed-Income fixtures
the archetype surfaces need.

`Cash` is mapped to `listed_bonds` and modelled as ADR-0079's **degenerate Fixed-Income
case** (balance plus running yield): `ytm ≈` the short end of the curve, `eff_duration ≈
0.1y`, `oas = NULL`, `convexity ≈ 0`, rating mostly `AAA`/`NR`, maturity 100 % in the
`0-1y` bucket, and the "coupon" interpreted as a running yield. This keeps cash on the
single Fixed-Income code path rather than introducing a fourth archetype now.

### 4. Sample-data coverage and return conventions (Variante A — NAV-preserving)

- **Coverage.** All listed instruments are fully equipped in the v26 workbook:
  `listed_equity` (A, B, C, H), `listed_bonds` (I, J, K, L, M), and the degenerate cash
  fund (T).

- **Variante A — NAV-preserving income split.** The existing daily NAV path is taken as
  the **ex-income price NAV; income flows are additive on top**. NAV *levels are not
  altered*, so portfolio scale (≈ €1.08 bn latest actual NAV) and the two intentional
  SAA breaches are preserved by construction. The TR-reconstruction invariant
  `r_t = (NAV_t + income_t) / NAV_{t-1} − 1`
  (`compute_cashflow_adjusted_return_series`, ADR-0079 §3 / ADR-0066) holds and yields a
  sensible total return.

- **Series frequencies.** `Bond Analytics` is a **monthly** series; `Rating Weights` and
  `Maturity Weights` are **quarterly**. These are genuine time series with real drift
  over the fund's life — not a single snapshot — because composition drift is
  analytically material to the Fixed-Income surface (and the reason ADR-0080 historised
  the weight tables). Income cashflows: **dividends quarterly**, **coupons
  semi-annual**.

- **Plan/actual axis.** Plan sheets are provided only where a plan/actual axis exists.
  NAVs already carry plan and actual; income gains both an `actual` **and** a `plan`
  sheet. The three reference tables have no plan/actual axis (only `basis`) and
  therefore **no plan sheet**.

- **Generator (built in the next prompt).** A deterministic, seeded script adapts the
  throwaway `liquid_spike.py` curve → spread → YTM → duration logic, reading the existing
  NAV columns plus the `interest rates` and `Benchmarks actual` series. **No real names
  appear anywhere** in the synthetic data.

---

## Rationale

**Why long for the three reference sheets, wide for income?** The wide date×investment
grid encodes exactly two dimensions. Bond analytics carries a metric dimension
(`ytm`/`eff_duration`/`oas`/`convexity`) and the weight ladders carry a bucket
dimension; forcing either into the wide idiom would mean one block of columns per metric
or per bucket, multiplying header complexity and breaking the dynamic-column-discovery
contract (ADR-0009). The tidy/long shape maps one workbook row to one table row on the
table's natural key, which is also how `Benchmark Mapping` (ADR-0061) already departs
from the wide idiom. Income, by contrast, is genuinely two-dimensional (a date and an
investment), so it stays in the proven wide cash-flow idiom and reuses that parser
unchanged.

**Why one type-derived income sheet, not separate dividend and coupon sheets?** The
income kind is fully determined by the resolved investment type — `listed_equity` pays a
`dividend`, `listed_bonds` pays a `coupon`. Splitting the workbook into a dividend sheet
and a coupon sheet would duplicate the column namespace, force the operator to place each
fund in the correct sheet, and add a validation surface for the case where a fund appears
in the wrong one. Deriving `flow_type` from the type the importer already resolves keeps
the workbook smaller and removes a class of operator error.

**Why optional-repo extension of `transform_upload_to_investments`, not a new
`transform_*_from_upload` method?** ADR-0061 added benchmarks through a *separate*
service method because benchmarks are a tenant-catalogue domain with their own FK
resolution and atomic-replace semantics, largely orthogonal to the per-investment
transform. The liquid-archetype reference data is the opposite: it is **per-investment**,
keyed on the very `investment_id` the main transform already resolves, and it is written
in the same pass as the NAVs and composition weights. Threading it through the existing
optional-repo seam — the identical pattern already used for region and sector weights —
keeps one transform authoritative for everything that hangs off an investment and avoids
re-resolving investments in a second method.

**Why Variante A (NAV-preserving)?** The shipped dataset has two load-bearing
properties that must not move: the ≈ €1.08 bn portfolio scale and the two intentional
SAA breaches that the limits surface demonstrates. Treating the existing NAV path as the
ex-income price NAV and adding income on top leaves both invariant by construction, while
still producing an honest, ADR-0066-correct total return on read. The alternative
(Variante B, a full TR-index split with ex-distribution NAV drops on pay dates) would
regenerate the NAV path and force a recomputation and re-verification of scale and both
breaches — fidelity we defer rather than buy now (roadmap #031).

**Why `basis="reported"` on every imported row?** The `basis` discriminator is the
ADR-0079 seam for a future holdings-aggregation source that will write `computed` rows
for the same natural keys. Everything that comes from an operator-supplied workbook is,
by definition, manager-reported, so the importer tags it `reported` uniformly. NAV
`basis` stays NULL because NULL already means `reported` for that table.

---

## Alternatives Considered

- **Wide idiom for the reference sheets too.** Encode bond analytics and the weight
  ladders as wide date×investment blocks, one block per metric/bucket. Rejected — it
  multiplies the header rows, couples the column count to the (fixed) taxonomy, and
  obscures the 1:1 mapping onto the tables' natural keys. The long form is the honest
  shape for a third dimension and has precedent in `Benchmark Mapping`.

- **Separate `Dividends` and `Coupons` sheets.** Give each income kind its own wide
  sheet. Rejected — the kind is derivable from the investment type, so separate sheets
  duplicate the namespace and introduce a "fund in the wrong income sheet" error class
  for no modelling benefit.

- **A dedicated `transform_liquid_archetypes_from_upload` service method** (the ADR-0061
  benchmark shape). Rejected for this domain — the reference data is per-investment and
  co-written with NAVs and composition in the same pass, so a second method would
  re-resolve investments and split one logical write across two entry points. The
  optional-repo extension keeps a single authoritative transform.

- **Variante B (full TR-index split) for the sample data.** Regenerate the showcase
  bonds with ex-distribution NAV drops on each pay date, so the reconstructed TR is
  duration-identity-reconciled end-to-end. Rejected for now — it would move the NAV path
  and require re-deriving portfolio scale and re-verifying the two SAA breaches
  computationally. Deferred to roadmap #031; ADR-0079 Test 1 (≤ 1e-6 reconciliation) is
  satisfied in the meantime by separate unit-test fixtures from the pure generator, not
  by the shipped dataset.

---

## Consequences

### Positive

- The demo tenant gains real liquid-archetype data, so the Total-Return-Equity and
  Fixed-Income surfaces defined in ADR-0079 render with genuine, drifting time series
  rather than empty frames.
- The import-format extension reuses two established patterns end-to-end: the wide
  cash-flow idiom for income and the dedicated-parser / `_parse_benchmark_mapping_sheet`
  pattern (ADR-0061) for the long reference sheets. No architectural deviation.
- The per-investment transform stays the single authoritative write path; the three new
  repos slot into the same optional seam as region/sector weights, so the change is
  opt-in and localised.
- `basis="reported"` on every imported row preserves the ADR-0079 provenance seam: a
  later `computed` holdings source is additive, with no schema or import change.
- Re-import is idempotent: single-row `upsert` on the natural keys means re-running the
  same workbook is a no-op for the three reference tables.

### Negative

- The `Credit` and `Cash` row-2 labels were being silently dropped; until the alias fix
  and the v26 workbook ship together, the demo dataset's Fixed-Income coverage is
  incomplete. The two are coupled and must land in the same step.
- Listed instruments now carry income inflows but no capital calls. Until ADR-0079 §1
  archetype routing is built, the legacy single-investment / Capital-Account view
  computes NaN multiples for them (guarded). See Neutral / Follow-ups #5.
- The sample workbook grows from 15 to 20 sheets and the generator must stay
  deterministic and seeded, which adds maintenance surface to the test-data pipeline.

### Neutral / Follow-ups

Variante A is a deliberate fidelity trade. The following imprecisions are accepted now
and scheduled for later refinement under **roadmap #031** (cross-referenced there):

1. **The shipped dataset is not duration-identity-reconciled.** The preserved daily NAV
   path has no ex-distribution drops on income pay dates, so reconstructed TR runs
   marginally "hot". ADR-0079 Test 1 (≤ 1e-6 reconciliation) is satisfied by **separate
   unit-test fixtures** from the pure generator, not by the shipped data. Later
   refinement: regenerate the showcase bonds via a full TR-index split (Variante B) and
   re-verify the two SAA breaches computationally.
2. **Cash modelled as `listed_bonds` with a degenerate profile** is a stopgap. A
   dedicated cash treatment (balance plus running yield, without a duration / OAS /
   rating ladder) is a later refinement.
3. **The `Credit` / `Cash` alias additions bend the alias-table discipline** ("extend
   only on real input"). They should be re-examined against real GP data that may carry
   differing labels.
4. **Equity sector/region composition stays a single snapshot** — anchored to the latest
   actual NAV date, as today, with no drift — until the same long-format time-series
   mechanism is extended to it.
5. **Interim incoherence of the legacy per-investment view.** Listed instruments carry
   income inflows but no capital calls, so the old Capital-Account / single-investment
   review computes NaN multiples for them (guarded) until the ADR-0079 §1 archetype
   routing is built.

---

## Implementation Notes

The next prompts implement this decision; this ADR names the exact seams they touch.

- **`modules/front_office/data_import.py`** — new sheet-category constant(s); the two
  income sheets join the investment-time-series category; the three reference sheets get
  dedicated parsers on the `_parse_benchmark_mapping_sheet` pattern. `_sheet_name_to_key`
  unchanged.
- **`services/data_normalization/investment_extractor.py`** — `extract_bond_analytics`,
  `extract_rating_weights`, `extract_maturity_weights`; the income-flow path emitting
  `ImportedCashflow` rows with type-derived `flow_type`; and the
  `_INVESTMENT_TYPE_ALIASES` additions (`credit`, `cash` → `listed_bonds`). Failures as
  `ImportRowError`, never thrown.
- **`services/investments/investment_service.py`** —
  `transform_upload_to_investments(...)` gains the three optional repository parameters;
  income flows reuse the existing cashflow write path; reference rows `upsert` on natural
  key with `basis="reported"`.
- **`web/routes/data_import.py`** — `_run_dry_run_extraction` and the commit path pass
  the three new repositories.
- **Repositories:** `core/repositories/investment_bond_analytics_repository.py`,
  `core/repositories/investment_rating_weights_repository.py`,
  `core/repositories/investment_maturity_weights_repository.py` (all merged with
  ADR-0079; consumed here through the transform).
- **Sample data:** the v26 workbook and its deterministic, seeded generator (adapting
  `liquid_spike.py`) are a **separate later prompt**, not part of this ADR.

## Compliance & Audit Relevance

- **Tenant isolation and RLS.** Every new row carries `tenant_id` and is written under
  the request's tenant context, RLS-policed exactly like the existing per-investment
  data (ADR-0035). The reference tables are audit-triggered as established by migration
  `b016`; the cashflow path is audited as today.
- **Provenance.** `basis="reported"` records that imported liquid-archetype data is
  manager-reported, distinct from a future `computed` holdings-aggregation source
  (ADR-0079 seam). The discriminator is visible and never silently overridden.
- **BAIT/VAIT.** Import provenance and tenant isolation are preserved; the change adds
  data domains through the existing audited import path without a new trust boundary.
  The Excel workbook remains the auditable source artefact for the imported series.
- **ISO 25010.** Functional Suitability (the archetype surfaces gain their inputs);
  Compatibility (additive sheets; the existing 15 sheets and their parsing are
  untouched); Reliability (idempotent `upsert`; partial-success row errors rather than
  whole-import aborts).

## References

- ADR-0079 (liquid-asset archetypes — the schema, taxonomies, and return conventions
  this ADR supplies the import path and sample data for)
- ADR-0080 (historised composition-weight tables — the time-series, `basis`-tagged
  pattern the reference sheets follow)
- ADR-0061 (Benchmarks & Attribution — the closest sibling: the import-format-extension
  and dedicated-parser pattern, and the sample-workbook precedent)
- ADR-0043 (investment-domain schema — signed-amount cashflow convention; partial-success
  `ImportRowError` discipline, §3)
- ADR-0009 (Excel import format — sheet-category model, dynamic column discovery)
- ADR-0059 (Excel import format naming hygiene)
- ADR-0066 (cash-flow-adjusted returns — the reused total-return primitive)
- ADR-0013 / ADR-0045 (analytics-layer purity — derive total return on read, never store)
- ADR-0035 (multi-tenant architecture, RLS)
- Roadmap **#031** (liquid-archetype test-data fidelity follow-ups)

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-06-16 | PortfoliFLOW project owner | Initial decision. |
