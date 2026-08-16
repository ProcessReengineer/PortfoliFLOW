# ADR-0045: Charts/Statistics Web Migration and Analytics-Service Foundation

- **Status:** Accepted
- **Date:** 2026-05-07
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, charts, statistics, analytics, plotly, sector-country, schema, phase-5

---

## Context

Phase 5 of the PyQt6 → Web migration is the **last phase in the
six-phase plan as originally scoped**, with the explicit caveat that a
follow-on Phase 6 will absorb the formal closure work (multi-user
onboarding, tool-trust per-role overlay, GUI migration onto Postgres,
PyQt6 sunset strategy, the `Accepted` promotions of ADR-0019, ADR-0033,
ADR-0039, and the migration completion report). Phase 5 itself is
narrowed to the **functional web completion**: the Charts/Statistics
surfaces that Phase 3 (ADR-0042) and Phase 4 (ADR-0043) deliberately
deferred, plus the sector- and country-breakdown schema that Phase 4
left as a P5-1 follow-up.

Four QT modules are functional today and have no equivalent on the web:

- `modules/front_office/charts.py` — investment-detail charts
  (Total Return, Cash Flows & NAV with Net Capital Gain, TVPI & DPI
  over time)
- `modules/front_office/statistics.py` — investment-universe
  comparison (key-metrics cards, correlation heatmap, distribution /
  risk / risk-return / autocorrelation tables)
- `modules/front_office/portfolio_optimizer.py` — Portfolio Analysis
  (efficient frontier on the investment level, with tangency portfolio,
  capital market line, min-variance, current portfolio overlay)
- `modules/investor_communication/portfolio_review.py` — six-tile
  portfolio and per-investment review reports

A fifth module, `modules/back_office/saa.py`, provides Strategic
Asset Allocation in the QT world. Its web equivalent already exists
from Phase 3 (ADR-0042): `asset_classes`, `saa_configurations`,
`saa_asset_class_inputs`, `saa_correlations`, plus the corresponding
web routes and Tabulator.js inline editing. Phase 5 does **not**
re-migrate SAA.

Phase 4 follow-up P5-1 (sector- and country-breakdown normalisation)
is a Phase-5 prerequisite rather than a parallel concern: the
Portfolio-Review treemaps for Country split and Sector split cannot be
served from data the web side does not have. P5-1 is therefore folded
into this ADR rather than tracked as an independent decision.

Three forces shape this ADR:

1. **Migration discipline over redesign.** The QT charts have been
   specified iteratively by the project owner against operational
   reality at a German Versorgungswerk. Re-specifying them in a
   chat-driven design discussion would produce charts that look right
   but are not what the owner needs. Phase 5 ports the QT charts to
   the web with the QT screenshots and code as the specification of
   record.

2. **Architectural cleanup in passing.** The QT modules read directly
   from the in-memory `DataStore` singleton
   (`core.data_store.get_data_store()`) and call matplotlib in the
   widget body, mixing data-access, calculation, and rendering in
   one place. The web side cannot use this pattern (no `DataStore`
   singleton; HTMX-served Plotly specs replace widget-embedded
   matplotlib renders). Phase 5 therefore extracts the calculation
   layer into a dedicated `services/analytics/` module which both
   surfaces will eventually consume — the web side immediately, and
   the QT side in Phase 6 when ADR-0033's GUI-on-Postgres step lands.

3. **Sector- and country-breakdown is normalisation work, not chart
   work.** P5-1 carries its own schema decision (per-tenant sectors
   versus global sector taxonomy; per-tenant or global country list)
   that is independent of the chart migration but must land before
   the Country-split and Sector-split treemaps can be served.

This ADR records all three decisions together because they share the
same underlying force: *Phase 5 is migration plus the minimum
architectural cleanup required to make the migration durable beyond
Phase 5.*

## Decision

### 1. Migration discipline as a Phase-5 directive

Phase 5 ports the four QT modules listed above onto the web stack
without redesigning them. The QT implementation under
`modules/front_office/` and `modules/investor_communication/` plus the
operating screenshots are the specification of record. Plotly-spec
generators under `services/chart_specs/` produce JSON descriptions
that are consumed by Plotly.js in the browser; the visual result is
required to match the QT screenshots subject to the differences
intrinsic to Plotly versus matplotlib (interactivity, hover affordances,
SVG-vs-canvas rendering, server-side spec generation).

Where matplotlib-to-Plotly translation is not 1:1 — primarily for
specialised marker shapes, axis decorations, and dark-theme styling —
the deviation is recorded in the relevant sub-strang implementation
prompt and resolved in favour of Plotly's idiomatic equivalent rather
than emulating matplotlib pixel-for-pixel. The `chart_theme.json`
substrate from Phase 3 (dark canvas, `#E8304A` red accent) is ported
to a Plotly `layout` template under `services/chart_specs/_theme.py`
(or analogous location) so all Phase-5 charts inherit the same
visual language.

This directive is binding on Sub-Stränge 5b through 5e. Sub-Strang 5a
(sector/country schema) is upstream of the chart migration and not
subject to it.

### 2. Sector- and country-breakdown schema (P5-1)

Phase 5 introduces four new entities. All are **tenant-scoped** with
RLS and audit triggers, *with the deliberate exception of `countries`*,
which is **global**.

- **`countries`** — global stammtabelle keyed on ISO 3166-1 alpha-2
  code (`iso_code` CHAR(2) PRIMARY KEY), with a `display_name` TEXT
  column and a `region_default` TEXT column carrying a coarse default
  region label (e.g. `'DACH'`, `'Western Europe ex-DACH/UK/Nordics'`,
  `'North America — USA'`) that mirrors the labels visible in the
  Portfolio-Review country treemap. Region labels are operational
  conventions, not part of any external standard; they are kept as a
  default that tenants may shadow per investment via the `region`
  free-text field on `investments`. The table is seeded by
  Alembic migration `b007` from a static `iso-3166-1-alpha-2.json`
  fixture committed under `services/data_normalization/fixtures/`.
  Migrations of country labels (e.g. ISO renamings, region
  re-conventions) are managed via further Alembic migrations rather
  than tenant-side data writes.
- **`sectors`** — tenant-scoped catalogue of sectors with a
  `(tenant_id, code)` UNIQUE constraint, `display_name`, and a
  `is_active` flag. FoF boutique sector taxonomies vary (some use
  GICS-like categories, some use PE-flavoured aggregations like
  "Tech-Buyout" / "Healthcare-Growth"); the per-tenant catalogue
  acknowledges this directly. Bootstrap installs a single
  "unclassified" sector per tenant on `portfoliflow bootstrap`,
  analogous to the Phase-4 unclassified asset class.
- **`investment_country_weights`** — tenant-scoped weight table with
  columns `investment_id` (FK to `investments`, ON DELETE CASCADE),
  `country_iso_code` (FK to `countries`), `weight_pct` (NUMERIC,
  validated to fall in `[0, 100]`). UNIQUE on
  `(investment_id, country_iso_code)`. The weights for a given
  investment do not need to sum to 100 (a fund can have unallocated
  exposure); enforcement is left to the chart-aggregation layer
  rather than a database constraint.
- **`investment_sector_weights`** — tenant-scoped weight table with
  columns `investment_id`, `sector_id` (FK to `sectors`), `weight_pct`.
  UNIQUE on `(investment_id, sector_id)`. Same non-summation rule as
  for country weights.

Both weight tables follow the Phase-4 audit and RLS conventions
without exception.

The deliberate **single global table** (`countries`) is justified by
three properties:

- ISO 3166 country codes are a public standard with no commercial,
  tenant-specific, or competitive content.
- The size of the table is bounded (~250 rows) and changes
  infrequently.
- Treating it as global removes the operational requirement to seed
  it per-tenant on every bootstrap, which would otherwise be a
  meaningful overhead. The existing per-tenant pattern is reserved
  for tables whose rows reflect tenant-specific business decisions
  (asset classes, sectors, SAA configurations, investments).

The `apply_tenant_rls(...)` helper is **not** applied to `countries`.
The schema-regression guard
(`tests/regression/test_rls_schema_invariants.py`) is extended to
record `countries` as the single allow-listed exception, with a
docstring on the test that explains why.

The `InvestmentExtractor` under `services/data_normalization/` is
extended to populate `investment_country_weights` and
`investment_sector_weights` from the per-investment split rows in the
V2 Excel `Attributes` sheet that Phase 4 left in the `data_upload_sheets`
JSONB. Country values that do not resolve to a known ISO code, and
sector values that do not resolve to an existing tenant sector, are
recorded as row-level errors in `InvestmentExtractionResult.errors`
following the Phase-4 partial-success convention. New sectors are
**not** auto-created on import; a sector that does not exist in the
tenant catalogue is treated as an error to be resolved by the
operator before re-import. This is conservative — it prevents Excel
typos from polluting the sector catalogue — and consistent with the
ADR-0043 §3 separation between authoritative configuration and
imported transactional data.

### 3. Analytics-service foundation

Phase 5 introduces a new top-level service module
`services/analytics/` that holds the calculation layer extracted from
the QT modules. The module is organised by domain rather than by
chart:

- `services/analytics/investment_returns.py` — per-investment
  returns: total-return time series from NAV history, net capital
  gain (kumulative Cashflows + NAV − kumulative Calls), rolling
  TVPI/DPI/RVPI, multiples-per-year aggregation. Consumes
  `InvestmentRepository`, `InvestmentNavRepository`,
  `InvestmentCashflowRepository`. Reuses
  `services.reporting.data_providers._calculations.compute_irr` for
  rolling-IRR-since-inception (Brent's-method IRR engine, Phase-4
  reuse).
- `services/analytics/statistics.py` — risk and distribution
  statistics: Sharpe ratio, maximum drawdown, mean return (daily and
  annualised), standard deviation (daily and annualised), variance,
  skewness, kurtosis (excess), median return, min return, lag-1
  autocorrelation. Operates on return series produced by
  `investment_returns.py`. Larger QT risk tables (VaR, CVaR, Ulcer
  Index, downside deviation) are deferred — sub-stream 5c migrates
  only the surface that the web Statistics page actually renders
  today.

  **Update 2026-05-12 — Phase 6 / Sub-stream 6F-3b-Plus:** This
  deferral has been reversed. The extended risk tables (VaR 90 / 95
  / 99, CVaR 95, Ulcer Index, Downside Deviation, Sortino Ratio,
  Autocorrelation lags 2–4) are now part of the web Statistics
  surface, with Qt-consistency tests at 1e-12 in
  `tests/services/analytics/test_statistics.py`. See
  `docs/phase-6-block-1-6f-3b-plus-acceptance-checklist.md`.
- `services/analytics/correlation.py` — pairwise correlation matrix
  computation across a configurable investment universe. Output is
  the substrate for the Statistics-module heatmap and (in Phase 6+
  re-kickoff) for any cross-investment optimisation work.
- `services/analytics/portfolio_aggregation.py` — portfolio-level
  roll-ups: NAV by asset class over time, NAV by country, NAV by
  sector, NAV by vintage, portfolio-level cashflow aggregation,
  portfolio-level rolling multiples. Consumes
  `investment_country_weights` and `investment_sector_weights`
  from Sub-Strang 5a.
- `services/analytics/efficient_frontier.py` — investment-universe
  efficient frontier. Reuses the SLSQP optimiser already in service
  for the SAA module (Phase 3). Inputs are investment-level expected
  returns and the correlation matrix from `correlation.py`; outputs
  are the frontier curve, tangency portfolio, min-variance portfolio,
  and capital-market-line geometry.

The cross-cutting design property is that every analytics function
takes data **as arguments** — typically pandas DataFrames or numpy
arrays — and returns plain Python data structures. None of the
analytics functions reach into the database directly; that is the
service-layer caller's responsibility (e.g. `InvestmentService` in
`services/investment_service.py` for the web side, or a future
`PortfolioAnalyticsService` orchestrator). This separation makes the
analytics layer trivially testable, deterministic, and free of
tenant-context concerns.

`services/chart_specs/` (Phase-3 etablished) is the consumer that
transforms analytics output into Plotly JSON specs. New spec
generators are added per QT-chart family without modifying the
analytics module — chart-spec changes do not require analytics-layer
changes and vice versa.

### 4. Phase-6 architectural promise

The analytics layer is designed for **dual consumption**:

- The web side (Phase 5) uses it directly via FastAPI route handlers
  that call into `InvestmentService` or analogous orchestrators,
  which in turn delegate calculation to `services/analytics/*`.
- The QT side (Phase 6, ADR-0033 follow-up) will switch from the
  in-memory `DataStore` singleton to repository-backed reads from
  Postgres, and will consume `services/analytics/*` directly without
  reimplementation. The QT widgets retain matplotlib for rendering;
  only the data-and-calculation pipeline is shared.

This is the architectural payoff that justifies the analytics-layer
extraction during Phase 5 rather than after. ADR-0033 (currently
`Proposed`) will be promoted to `Accepted` in Phase 6 with a revision
note that records the Phase-5 analytics-foundation work as the
Phase-6 enabling step.

## Consequences

**Positive:**

- The four QT charts modules become available on the web with
  visual and numerical fidelity to the QT versions.
- Sector and country breakdowns become first-class columns on the
  web side, enabling P5-1 follow-up consumers (LP reports,
  cross-investment exposure analysis).
- `services/analytics/` becomes the single source of truth for
  return, risk, statistics, correlation, aggregation, and frontier
  calculations across both surfaces. Future analytics enhancements
  (e.g. type-specific analytics, P5-2) extend one module instead of
  two.
- The Phase-3 `services/chart_specs/` pattern is exercised against a
  larger surface, validating its scalability.
- Phase 6's GUI-on-Postgres migration becomes substantially smaller:
  the QT widgets need only swap their data source; the calculation
  layer is already shared.

**Negative / accepted trade-offs:**

- A second migration (matplotlib → Plotly) is performed during what
  is fundamentally a data-layer migration phase. The cost is
  deliberate: doing it now means there is no separate
  "Charts/Statistics-on-Web" project later, and no period during
  which the web side has data but no visualisations.
- The `countries` table is the first global table in the schema. A
  precedent is being set; the schema-regression guard's allow-list
  must be maintained going forward.
- The QT side carries a temporary inconsistency: `core.data_store`
  remains the GUI's data source through Phase 5, while the web side
  uses repositories. This widens the strangler asymmetry that
  ADR-0039 records, until Phase 6 closes it. Demo discipline (per
  the Phase-4 demo-stability note) continues to mean: demo on the
  web side, freeze the GUI on `main`.
- New sectors are not auto-created on Excel import. Operators
  encountering an unknown sector must add it to the catalogue
  manually before re-importing. This is conservative; an
  auto-creation mode can be added additively if operational pressure
  surfaces.

**Notes:**

- Plan-cashflow visualisation in Phase 5 is restricted to plan rows
  already present in `investment_cashflows` (`flow_kind = 'plan'`).
  Phase 5 does **not** introduce a forecasting model; the QT modules
  do not have one either. A scenario-slider-driven forecasting
  capability is explicitly deferred to a post-refactoring chat
  (post-Phase-6), per the project owner's direction.
- Plan versioning (P5-3) remains deferred. No Phase-5 chart consumes
  multi-version plan data; Phase-4 audit-log reconstruction remains
  the forensic answer for "what did the plan look like last quarter?"
- Currency stammtabelle and FX handling (P5-4) remain deferred. All
  Phase-5 charts assume `currency = 'EUR'` at the visualisation
  level; multi-currency aggregation is not exercised.
- Multi-asset-class weights (P5-5) remain deferred. Investments
  remain 1:1 to asset classes for the Asset-class-level aggregation
  in Sub-Strang 5e.

## Sub-Strang Map

This ADR is implemented across seven sub-stränge. Each sub-strang has
a separate implementation prompt for Claude Code; the prompts inherit
this ADR as their architectural specification.

| Sub-Strang | Title                                                                       | Depends on             |
|------------|-----------------------------------------------------------------------------|------------------------|
| 5a         | Sector/Country schema and extractor extension (P5-1)                        | Phase-4 head           |
| 5b         | Analytics-service foundation and `front_office/charts.py` migration         | 5a (theme), Phase-4    |
| 5c         | `front_office/statistics.py` migration                                      | 5b                     |
| 5d         | `front_office/portfolio_optimizer.py` migration (Portfolio Analysis)        | 5b, 5c (correlation)   |
| 5e         | `investor_communication/portfolio_review.py` migration (six-tile reviews)   | 5a, 5b, 5d             |
| 5f         | Phase-5 consolidation (ADR-0045 to `Accepted`, theme port, hygiene)         | 5a–5e                  |
| 5g         | Phase-5 acceptance report and Phase-6 kickoff preparation                   | 5f                     |

## Test Discipline

The Phase-3 / Phase-4 test discipline carries over without exception:

1. Schema-regression guard parametrised over the four new tables
   (`countries`, `sectors`, `investment_country_weights`,
   `investment_sector_weights`); `countries` allow-listed for the
   non-RLS rule with explanatory docstring.
2. Repository-level tests for `SectorRepository`,
   `InvestmentCountryWeightsRepository`,
   `InvestmentSectorWeightsRepository` under the unprivileged
   `portfoliflow_app` role.
3. Cross-tenant isolation tests for each new tenant-scoped table.
4. Audit-log completeness tests for INSERT / UPDATE / DELETE on each
   new tenant-scoped table.
5. Extractor unit tests for sector and country split parsing,
   including unknown-code error paths.
6. Analytics-layer unit tests with deterministic input fixtures.
   Numerical functions (rolling IRR, Sharpe, MDD, correlation,
   skewness, kurtosis) compared against precomputed reference values
   (Excel goldstandard where available; pandas / numpy / scipy
   reference implementations otherwise).
7. Chart-spec generator tests asserting structural invariants of the
   produced Plotly JSON (axis types, trace counts, colour mappings)
   rather than full visual diffs.
8. Web HTTP tests for the new routes via `httpx.AsyncClient`,
   including foreign-tenant 404 behaviour.
9. Performance sanity check on the Portfolio-Review route at the
   Phase-4 baseline scale (100 investments × 20 NAVs × 50 cashflows):
   end-to-end render below 300 ms.
10. Web-no-`PersistentDataStore`-regression-guard remains active.

## Revision History

- **2026-05-07** — initial draft. Status `Proposed`. To be promoted
  to `Accepted` at Phase-5 consolidation (Sub-Strang 5f) once all
  preceding sub-stränge have shipped and the acceptance report
  (Sub-Strang 5g) is signed off by the project owner.
- **2026-05-07** — Phase-5 implementation complete. Sub-Stränge
  5a–5e shipped (sector/country schema, charts module, statistics
  module, portfolio analysis, portfolio review six-tile reports).
  Analytics-service foundation under `services/analytics/` is in
  place with five submodules (`investment_returns`, `statistics`,
  `correlation`, `efficient_frontier`, `portfolio_aggregation`).
  ~200 net new tests; total test count 1220. Status promoted to
  `Accepted`.
- **2026-05-07** — Phase-5 acceptance report produced
  (`docs/phase-5-acceptance-report.md`, Sub-Strang 5g). Final test
  count: 1218 passed, 2 skipped (+281 net new vs. Phase-4 baseline of
  939). Performance measured at the 100-investment scale: per-
  investment routes under their thresholds (charts 63.6 ms;
  single-investment review 67.8 ms); universe-wide routes over their
  estimated thresholds (`/statistics` 947.5 ms vs. 500 ms target;
  `/portfolio-review` overview 1509.9 ms vs. 300 ms target) with root
  cause traced to an N+1 query loop in the service layer (recorded as
  P6-H follow-up). The `POST /portfolio-analysis/compute` 26 s
  reading is a synthetic-data optimiser-failure artifact, not a
  production property — verified by the QT-consistency suite and by
  the realistic-input demo in the acceptance report §6.3 (recorded as
  P6-I follow-up). Awaiting project-owner walkthrough sign-off and
  `phase-5-complete` tag.
- **2026-05-07** — Hygiene follow-up: analytics-layer regression
  guard added under `tests/regression/test_analytics_layer_pure.py`,
  completing the automation of the §3 / acceptance-report §5
  contract that the analytics layer remains DB-, FastAPI-, and
  Qt-free. No behavioural change.
