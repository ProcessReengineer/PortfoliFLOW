# ADR-0099: Multi-Currency Model — Functional Currency, FX Rates, and the Conversion Boundary

- **Status:** Accepted
- **Date:** 2026-07-10
- **Deciders:** PortfoliFLOW project owner
- **Tags:** schema, analytics, fx, currency, import, market-data, engine-contract, phase-8

---

## Context

PortfoliFLOW models the **position currency** of every instrument
consistently: `investments.currency` is mandatory (ISO 4217, free
text — the currency stammtabelle remains deferred per P5-4), and
`investment_navs`, `investment_cashflows`, `instrument_prices`, and
`position_transactions` each carry their own `currency` column. The
ingest paths defend this invariant strictly: ADR-0092 §5 and
ADR-0097 §5 reject any price or transaction whose currency differs
from the investment's currency (`CurrencyMismatchError`) — writes
are refused, never converted.

What the system lacks is the counterpart concept: the **functional
currency** of the portfolio — the currency in which the portfolio
as a whole is expressed and in which every aggregate figure is
reported. Today this is an implicit, hard-coded assumption:

- `tenants` carries no currency column.
- `portfolio_aum.aum_eur` names EUR in the schema itself.
- `OverviewKpis` documents "All monetary figures are EUR"
  (ADR-0067); Phase-5 documentation records that all charts assume
  `currency = 'EUR'` at the visualisation level.
- `_portfolio_nav_series` in
  `services/analytics/portfolio_aggregation.py` sums per-investment
  NAV series **numerically**, with no conversion. A USD fund's
  100 m NAV contributes 100 m to the EUR total. The same applies to
  the cashflow aggregation feeding IRR / TVPI / DPI and to the
  ADR-0055 cash residual.
- The only existing treatment is detection without consequence: the
  `portfolio_nav_series` bundle surfaces a `"mixed currencies: …"`
  line in its model-facing summary so Shirley can disclose the mix
  in prose.

No FX rates exist anywhere in the system: no table, no
`SeriesKind`, no import sheet. ADR-0009 §Follow-ups explicitly
reserved "FX rates" as a future market-reference sheet, so the
extension point (`MARKET_REFERENCE_SHEETS` in
`excel_workbook_loader.py`) exists and has been exercised once
before (`Benchmarks actual`, ADR-0061).

The sample data (Fund B in USD, Fund C in GBP) already exercises
the gap. Scenario and cashflow planning (roadmap) must not be built
on aggregates that add USD nominally into EUR.

Three currency concepts must be distinguished throughout:

1. **Functional currency** — the portfolio's reporting currency
   (per tenant; EUR for the reference deployment).
2. **Position currency** — the currency an individual investment is
   denominated and settled in (already modelled).
3. **Reference currency** — the base currency of the FX-rate
   dataset, against which all stored rates are quoted. A property
   of the FX data, **not** of the portfolio; the two may coincide
   (EUR/EUR in the reference deployment) but are distinct concepts.

## Decision

### 1. Functional currency as a tenant attribute

`tenants` gains `functional_currency TEXT NOT NULL DEFAULT 'EUR'`
(ISO 4217, upper-cased, application-validated; no stammtabelle —
consistent with the deferred P5-4 posture). Every aggregate figure
the system reports is expressed in the tenant's functional
currency. The tenant-equals-portfolio premise of the current
architecture makes the tenant the correct carrier; a future
multi-portfolio-per-tenant model would move the column, not the
concept.

### 2. `fx_rates` table — one rate per (currency, date), quoted against a reference currency

New tenant-scoped table `fx_rates`:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `tenant_id` | UUID FK → tenants | RLS-scoped like all tenant data (ADR-0078) |
| `as_of_date` | DATE NOT NULL | rate date |
| `currency` | TEXT NOT NULL | ISO 4217; the currency being priced |
| `rate_to_reference` | NUMERIC(20, 10) NOT NULL | value of **1 unit of `currency`** expressed in the reference currency |
| `reference_currency` | TEXT NOT NULL | stored explicitly for auditability; constant per tenant in practice |
| `source` | TEXT NOT NULL | e.g. `excel`, `ecb`, `yahoo` |
| `ingest_origin` | TEXT NOT NULL | ADR-0092 Excel-precedence semantics apply unchanged |
| `created_by` / `created_at` / `updated_at` | | standard audit columns |

Constraints: `UNIQUE (tenant_id, currency, as_of_date)`,
`CHECK (rate_to_reference > 0)`,
`CHECK (currency <> reference_currency)` (the identity rate is
never stored; it is an application-level short-circuit).

**Quoting convention (normative):** `rate_to_reference` is the
price of one unit of `currency` in the reference currency
(EUR-based deployment: `USD → 0.92` means 1 USD = 0.92 EUR).
Conversion between two non-reference currencies triangulates:
`amount × rate(from) / rate(to)`. Storing rates against a single
reference keeps the dataset linear in the number of currencies, per
the triangulation rationale.

**Tenant-scoped, not global.** FX rates are objective market data,
but tenant scoping is chosen deliberately: it preserves RLS
uniformity (ADR-0078), and the Excel-over-live precedence of
ADR-0092 (`ingest_origin`) is inherently tenant-specific — two
tenants may legitimately maintain different rate sources.

**Reference currency choice (non-normative default):** EUR, sourced
from ECB daily reference rates (free, revision-stable,
supervisory-grade, published as EUR/XXX pairs — no transformation
needed). The architecture treats the reference currency as data
configuration, not code.

### 3. FX service — pure, stateless, typed failure

New `services/fx/` exposing an `FxConversionService` under the
ADR-0013 analytics contract (pure, stateless, no I/O of its own; it
receives loaded rate frames from the repository layer):

- `convert(amount, from_currency, to_currency, as_of_date)` —
  triangulates via the reference currency.
- **Identity short-circuit:** `from == to` returns the amount
  unchanged without any rate lookup. This is the
  backwards-compatibility guarantee: a pure-EUR portfolio operates
  with zero FX rows and zero behavioural change.
- **Carry-forward semantics:** the rate applied is the latest
  at-or-before `as_of_date`, mirroring ADR-0060 NAV carry-forward
  and the existing `_latest_at_or_before` idiom (ECB series have
  holiday gaps).
- **Typed failure:** a required rate that cannot be resolved raises
  `MissingFxRateError` (new member of the ADR-0005 hierarchy)
  naming currency and date. There is no silent 1:1 fallback,
  anywhere.

### 4. Conversion boundary — between data assembly and pure analytics

Conversion into the functional currency happens exactly once, at
the seam where the data-assembly layer builds
`nav_history_by_investment` and `cashflows_by_investment` for the
analytics functions (the `PortfolioReviewService` load path). The
analytics layer (`services/analytics/`) remains **currency-agnostic
with a single-currency contract**: every monetary series it
receives is already in one (the functional) currency. ADR-0013
purity is untouched.

Conversion is **point-in-time**: `NAV(t)` converts at `rate(t)`
(carry-forward), each cashflow at the rate of its flow date. The
resulting portfolio IRR / TVPI / DPI are functional-currency
figures **including FX effect** — the correct semantics for the
functional view. UI surfaces should note that multiples in
functional currency may differ from the same investment's multiples
in position currency; the Single Investment Review remains in
position currency (its axis labelling already does this).

Detection becomes handling: the `"mixed currencies"` summary line
remains as disclosure, but aggregates are henceforth correct rather
than nominal.

### 5. Data supply

- **Excel:** new market-reference sheet `FX rates` (wide format:
  columns = currency codes, rows = dates, values =
  `rate_to_reference`) added to `MARKET_REFERENCE_SHEETS`,
  canonical key `fx_rates` — the ADR-0009 §Follow-ups extension
  point, exercised exactly as ADR-0061 did for benchmarks. This is
  the v1 supply path.
- **Live (deferred, enabled):** new `SeriesKind.FX_RATE` plus a
  capability entry (ADR-0091). The natural adapter is the ECB SDMX
  API (keyless, EUR-based); Yahoo `EURUSD=X`-style tickers are a
  possible interim. Nothing downstream changes when an adapter
  lands — the port/DTO/capability seams absorb it.

### 6. Explicitly out of scope

- **FX hedging / currency overlay** — hedge instruments, hedge
  ratios, and hedged-vs-unhedged exposure views are a separate,
  later decision.
- **Statistics / frontier currency contract** — total-return-based
  statistics differ between local-currency and functional-currency
  measurement for foreign-currency positions. The
  statistics/SAA/archetype layer keeps its current behaviour;
  defining its currency contract is named as a follow-up, not
  solved here.
- **Cash-in-foreign-currency positions** — the modelling of
  explicit `Cash USD` / `Cash GBP` investment rows and the
  redefinition of the ADR-0055 residual is a companion decision
  (planned ADR-0100); this ADR only provides the conversion
  machinery it depends on.
- **Currency stammtabelle** — remains deferred (P5-4).

## Rationale

- The position-currency half of the model and the strict
  no-conversion-on-write discipline (ADR-0092/0097) mean the stored
  data is already clean per position; conversion belongs on the
  read/aggregation side, applied once at a named seam.
- Placing the boundary in front of the analytics layer keeps
  ADR-0013 intact and gives analytics a simpler, *stronger*
  contract (single currency) instead of threading currency
  awareness through every aggregation function.
- Point-in-time conversion is the only choice that produces a
  meaningful functional-currency IRR; converting everything at the
  latest rate would silently remove the FX effect from history.
- A single reference currency with triangulation keeps FX data
  linear in the number of currencies; storing it per-row
  (`reference_currency`) keeps every rate self-describing for
  audit.
- The identity short-circuit plus typed `MissingFxRateError` gives
  the two properties the system must have simultaneously:
  zero-impact on single-currency tenants, and loud failure instead
  of silently wrong numbers on multi-currency tenants with missing
  rates.

## Alternatives Considered

- **Convert on write (store everything in functional currency):**
  Rejected — destroys position-currency truth, contradicts
  ADR-0092/0097's refusal to convert, makes rate revisions
  unrepairable, and breaks the Single Investment Review's
  position-currency view.
- **Convert inside each analytics function:** Rejected — spreads
  currency awareness across every aggregation, violates the
  single-responsibility shape of ADR-0013, and multiplies the
  places a missing rate can be mishandled.
- **Global (non-tenant) fx_rates table:** Rejected for now — breaks
  RLS uniformity and cannot express per-tenant source precedence
  (ADR-0092). Revisit if rate storage duplication ever becomes a
  real cost.
- **Full currency pair table (base, quote):** Rejected —
  quadratic data volume for no expressiveness gain; triangulation
  over a reference currency is standard practice and sufficient.
- **Silent 1:1 fallback for missing rates:** Rejected outright —
  this is precisely the wrong-number failure mode the ADR exists to
  eliminate.

## Consequences

### Positive

- Portfolio aggregates, KPIs, and the ADR-0055 residual become
  correct for multi-currency portfolios.
- Pure-EUR tenants are provably unaffected (identity short-circuit;
  no FX data required).
- The scenario / cashflow-planning roadmap gains a sound monetary
  foundation.
- FX exposure becomes reportable (Front-Office tiles, planned under
  ADR-0067/0072 patterns).

### Negative

- Multi-currency tenants acquire a data-maintenance duty: FX rates
  must cover every (currency, date-range) their positions need, or
  aggregation fails loudly.
- Triangulated rates compound two quotes; NUMERIC(20, 10) bounds
  but does not eliminate rounding discussion in reconciliation.
- Functional-currency multiples include FX effect; users comparing
  against GP-reported (position-currency) multiples will see
  differences that require explanation in the UI.

### Neutral / Follow-ups

- ADR-0100 (planned): explicit foreign-currency cash positions —
  extend the `investment_type` CHECK by `'cash'`, redefine the
  ADR-0055 residual as *cash in functional currency*.
- Rename refactor `*_eur` → `*_functional` across DTOs and
  `portfolio_aum.aum_eur` (ADR-0044 precedent for renames);
  deliberately not bundled here.
- Statistics/SAA currency contract (see Out of scope).
- Glossary v3 entries: *functional currency*, *position currency*,
  *reference currency* (ADR-0002 / ADR-0084 lineage).
- ECB SDMX adapter under ADR-0091 when live FX supply is wanted.

## Implementation Notes

- Migration `b026`: `tenants.functional_currency` + `fx_rates`
  table + RLS policy.
- `core/models/fx_rate.py`, `core/repositories/fx_rate_repository.py`
  following the b006-era repository idiom.
- `services/fx/__init__.py`, `services/fx/conversion.py`
  (`FxConversionService`, `MissingFxRateError`).
- `services/data_normalization/excel_workbook_loader.py`: add
  `"FX rates"` to `MARKET_REFERENCE_SHEETS`; extractor writes
  through the fx repository with `ingest_origin='excel'`.
- Conversion seam: the `PortfolioReviewService` /
  `investment_service` load path converts NAV and cashflow frames
  before they reach `services/analytics/portfolio_aggregation.py`;
  analytics signatures unchanged.
- Tests: identity short-circuit (EUR-only tenant, zero FX rows);
  triangulation (JPY→GBP via EUR); carry-forward over rate gaps;
  `MissingFxRateError` on uncovered currency/date; mixed-currency
  aggregation golden figures against the v30 sample workbook
  (Fund B USD, Fund C GBP); ADR-0092 precedence for `fx_rates`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Functional
  correctness (aggregates in a defined currency), Reliability
  (typed failure over silent wrong numbers), Auditability (every
  rate row self-describes source, origin, and reference currency).
- **Audit evidence:** `fx_rates` rows with `source` /
  `ingest_origin`; conversion-seam code; golden-figure tests.
- **DORA framing:** FX data supply is an ICT third-party dependency
  once the live adapter lands; the Excel path is the documented
  fallback.

## References

- ADR-0005 (typed exception hierarchy — `MissingFxRateError`)
- ADR-0009 §Follow-ups (FX rates reserved as market-reference sheet)
- ADR-0013 (analytics pure and stateless — the contract the
  conversion boundary protects)
- ADR-0055 (cash as residual — redefined by planned ADR-0100)
- ADR-0060 (NAV carry-forward — the at-or-before precedent)
- ADR-0061 (benchmarks — prior exercise of the market-reference
  extension point)
- ADR-0067 / ADR-0072 (Overview KPI strip and chart row — tile
  landing zone)
- ADR-0078 (RLS enforcement — tenant scoping of `fx_rates`)
- ADR-0091 / ADR-0092 (market-data port, ingest contract, Excel
  precedence)
- ADR-0097 (position model — currency-equality write rule)

---

## Revision History

| Date       | Author                     | Change         |
|------------|----------------------------|----------------|
| 2026-07-10 | PortfoliFLOW project owner | Initial draft. |
| 2026-07-11 | PortfoliFLOW project owner | Accepted against the shipped code. Implemented 2026-07-10: migration `b026` (functional currency + `fx_rates`), the `services/fx/` package (`conversion.py`, `functional_currency.py`), and the conversion boundary at the review and limits seams (blocks 1–3). |
