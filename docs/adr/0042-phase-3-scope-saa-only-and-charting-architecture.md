# ADR-0042: Phase 3 Scope — SAA-Only Domain Schema and Plotly-First Charting

- **Status:** Accepted
- **Date:** 2026-05-05
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, domain-schema, charting, scope, architecture

---

## Context

Phase 3 of the PyQt6 → Web migration ports the **Strategic Asset
Allocation (SAA)** module — the first true investment-management
domain to land in the web variant — alongside the foundational
visualisation stack (interactive charts, editable tables) that all
subsequent migrated modules will reuse.

Two scope decisions had to be made before Phase 3 implementation
could begin, and the answers to one constrain the answers to the
other:

1. **Domain-schema scope.** SAA references *asset classes* with
   forward-looking expectations (expected return, volatility,
   correlation matrix), not individual investments with historical
   time-series data. The full investment-domain schema (per-investment
   tables for equities, PE funds, real estate, etc., backed by NAVs,
   cashflows, attributes) is structurally adjacent but is not strictly
   required to ship a working SAA web surface. The question was
   whether Phase 3 builds the full investment-domain schema — incurring
   substantial up-front design work without a concrete consumer — or
   confines itself to the SAA-specific subset.

2. **Charting architecture.** ADR-0037 §4 designated Plotly.js as the
   web charting standard. A separate forward-looking concern surfaced
   during Phase 3 planning: a planned (Phase 5+) Shirley capability in
   which the assistant generates dynamic matplotlib code at tool-call
   time to answer ad-hoc visualisation requests
   ("show me the IRR distribution of 2018 vintages as a box plot").
   This raised the question of whether Plotly should remain the sole
   web charting engine, or whether a unified matplotlib-based path —
   serving both standard charts and Shirley's dynamic charts — would
   better preserve future optionality.

Both decisions are scope-shaping rather than security-critical, but
both have audit-relevant downstream effects: the domain-schema
decision determines what data lives in Postgres at the end of Phase 3,
and the charting decision determines the migration trajectory for
every subsequent visual surface.

This ADR records both decisions together because the underlying force —
*deliberate scope discipline during a multi-phase migration* — is
the same in both cases.

## Decision

### 1. SAA-only domain schema in Phase 3

Phase 3 introduces a **minimum viable domain schema** that supports
the SAA workflow only. The full investment-domain schema is
deliberately deferred to Phase 4.

The Phase-3 schema consists of four tables, all tenant-scoped, all
RLS-protected via the standard `apply_tenant_rls(...)` helper, all
audit-logged via the `audit_trigger_function` from b001:

- **`asset_classes`** — per-tenant catalogue of asset-class
  definitions (code, display name, optional description). Tenants
  curate their own vocabulary; there is no global asset-class table.
- **`saa_configurations`** — top-level SAA configuration entity
  (name, risk-free rate, frontier-point count, `is_active` flag,
  audit columns). Exactly one configuration per tenant may have
  `is_active = TRUE` at any time, enforced by a partial unique index.
- **`saa_asset_class_inputs`** — per-configuration, per-asset-class
  inputs: expected return, volatility, minimum weight, maximum
  weight. Equivalent to one row of the PyQt6 SAA widget's input table.
- **`saa_correlations`** — per-configuration upper-triangle correlation
  values, stored as `(asset_class_a_id, asset_class_b_id, correlation)`
  triplets. The diagonal (always `1.0`) and the lower triangle
  (mirror of the upper triangle) are not stored.

The Phase-4 investment-domain schema decision (flat polymorphic table
vs. one table per investment type vs. common base + type-specific
side tables) is **explicitly not pre-empted** by this ADR. Phase 4
will choose with the benefit of a concrete second consumer
(Phase-4 GUI on Postgres, Phase-5 Charts/Statistics modules).

### 2. SAA optimisation outputs are not persisted

Optimisation results (efficient frontier, tangency portfolio,
minimum-variance portfolio, random portfolio cloud, capital market
line) are computed on demand from the persisted configuration. No
`saa_optimization_runs` table is introduced.

This honours YAGNI: SAA optimisation runs in sub-second to low-second
time for realistic asset-class counts (5–15), the analytics engine
(`analytics/portfolio_optimizer.py`) is pure and deterministic, and
the feature is expected to be exercised at most a few times per week
per user. Persisting outputs would create a synchronisation duty
(invalidate on configuration change) without compensating value.

If Phase 5 or later introduces audit requirements that ask
"which SAA output was binding on date X?", an `saa_optimization_runs`
table can be added additively without retrofitting the schema.

### 3. Cross-module API surface on `SAAService`

The `SAAService` class (in `services/saa/`) is structured with a
**deliberately documented public API surface** that other modules
will consume. The intent is that future modules (cash-flow projection
with allocation-limit checks, front-office reporting that overlays
target vs. actual allocation, Shirley) read SAA configuration data
*through* this surface rather than reaching into SAA repositories
directly.

Phase 3 does **not** implement these cross-module methods (no
consumer exists yet, and YAGNI applies). The architectural
preparation is structural, not behavioural: the service is laid out
so that additive methods like `get_active_configuration()`,
`get_constraints_for_compliance_check()`, and
`get_configuration_by_name()` can be added in 30–50 lines plus tests
when the first consumer arrives, without refactoring existing code.

### 4. Plotly remains the standard web charting engine

ADR-0037 §4's designation of Plotly.js as the web charting standard
is reaffirmed. Phase 3 implements all SAA charts (efficient frontier
with hover tooltips, tangency / minimum-variance markers, random
portfolio cloud, capital market line) as Plotly figures generated
server-side from the canonical theme.

The server-side spec generator lives at `services/chart_specs/`. Each
spec module exports a pure function that returns a Plotly-figure
dict (data + layout + config) with the theme already applied. Routes
serialise the dict to JSON; the browser calls
`Plotly.newPlot(target, fig.data, fig.layout, fig.config)`. No
Plotly-specific theme file is introduced — `config/chart_theme.json`
remains the single source.

### 5. Matplotlib reserved as Shirley's tool-call backend in Phase 5+

A future Shirley capability — dynamic chart code generation,
sandboxed execution, returning a rendered image — is architecturally
reserved as a **new tool call** in Shirley's existing tool-registry
pattern (the same mechanism already used for `web_research`,
`generate_chart`, `datastore_tools`). The output of this tool call
will be a matplotlib-rendered PNG embedded into the chat surface
through a plain HTML `<img>` tag inside the same `chart-container`
CSS convention used for Plotly charts.

This is **not implemented in Phase 3** and is **not skeletoned** in
Phase 3 (no empty modules, no stub functions). The architectural
commitment is twofold:

- A unified `chart-container` CSS class (defined in Phase 3) will
  visually frame both Plotly figures (Phase 3) and matplotlib images
  (Phase 5+).
- `config/chart_theme.json` remains the single source of theme data
  for both engines. When Shirley's tool call is implemented, the
  matplotlib rendering path will reuse the existing
  `core/chart_helpers.apply_axes_theme()` from the PyQt6 codebase to
  apply the theme. A future regression test will assert that Plotly
  and matplotlib extract identical colour, font, and grid values from
  the same theme JSON.

Theme switching at runtime is **not** supported in either engine.
A theme change requires a server restart. This decision sidesteps
hot-reload coordination between two rendering engines.

## Rationale

### Why SAA-only domain schema

- **No concrete consumer for the broader investment schema in Phase 3.**
  The PyQt6 SAA widget reads exactly the data this Phase-3 schema
  models — manually entered asset-class expectations and correlations.
  It does not read individual investments. Designing the
  investment-domain schema now would be design-without-consumer, with
  high probability of a wrong choice between options (a)/(b)/(c) that
  must be unwound in Phase 4.
- **Phase-4 readiness is preserved.** Asset-class identifiers in
  Phase 3 use `UUID` primary keys with no semantic dependency on
  investment data. When Phase 4 introduces investments, the
  classification mapping `investment.asset_class_id →
  asset_classes.id` is a single foreign key, not a schema rewrite.
- **Side-by-side comparison in Sub-Strang 3d is achievable.** The
  PyQt6 SAA widget operates on the same data shape this schema
  models. Visual identity between PyQt6 and web is the explicit
  Phase-3 acceptance criterion; a broader schema would expand the
  comparison surface without contributing to that criterion.

### Why no optimisation persistence

- **Compute is cheap.** The analytics engine handles 5–15 asset
  classes with 100 frontier points and 5000 cloud portfolios in
  hundreds of milliseconds to a few seconds — well within an
  acceptable synchronous HTTP response budget for an
  infrequently-used feature.
- **Persistence creates a staleness duty.** If results are persisted,
  every configuration change must invalidate them or recompute
  asynchronously. Both options add code paths that have no value
  until an audit requirement materialises.
- **Identical compute in both worlds simplifies the acceptance
  diff.** The PyQt6 widget recomputes on every "Compute" click; the
  web variant recomputing on every detail-view request makes the
  numerical comparison trivial — any difference is a numerical
  difference, never a stale-data difference.

### Why Plotly for standard charts and matplotlib for Shirley

The earlier preference for "matplotlib everywhere" was reconsidered
on three grounds:

- **Standard charts are exploration surfaces; Shirley's charts are
  snapshot answers.** A user opens the SAA detail page, hovers over
  the efficient frontier, zooms in on the tangency neighbourhood —
  these are interactions. A user asks Shirley "show me the IRR
  distribution as a box plot" and receives an image — this is a
  one-shot answer. The two use cases benefit from genuinely
  different rendering choices: interactivity for the former,
  open-ended graphical expressivity for the latter.
- **The expected frequency profile.** Standard charts are viewed in
  every login session by every active user; Shirley's dynamic charts
  are exceptional cases ("can you make sense of this?"). Architecture
  should be optimised for the common case.
- **Tool-call shape is the natural home.** Shirley already has a
  tool-registry pattern with established trust gating, parameter
  validation, and result handling. A dynamic-chart tool call slots
  into this pattern as one more tool, not as a parallel architecture.

The unified `chart-container` CSS plus the single `chart_theme.json`
source preserve visual consistency without forcing a single rendering
engine.

### Why a deliberately-documented cross-module API on `SAAService`

The cash-flow projection module (Phase 5+) will need allocation
limits derived from the active SAA configuration. Front-office
reporting may want to overlay target vs. actual allocation. Shirley
may need read access to SAA constraints to answer compliance
questions. All three are *future* consumers; building any of them
now would be premature.

But anticipating the *shape* of the consumption pattern — read-only
access to a stable DTO contract, unaware of internal repository
factoring — is cheap in Phase 3 and expensive to retrofit. The
service is laid out as if these consumers existed; only their
methods are missing. When the first consumer arrives, adding the
methods is a localised additive change.

## Alternatives Considered

### Domain-schema scope

- **Full investment-domain schema in Phase 3.** Rejected because no
  concrete consumer exists in Phase 3, the design space is genuinely
  contested (flat polymorphic / table-per-type / hybrid), and a wrong
  choice would compound through Phase 4. Phase 4 will have a
  second concrete consumer (the migrated GUI) that disambiguates the
  options.
- **No new domain tables in Phase 3 — extend `data_upload_sheets`
  consumption only.** Rejected because asset-class expectations are
  not data that arrives via Excel uploads. The PyQt6 SAA widget
  receives these inputs through manual entry, and the web equivalent
  must do the same.
- **Persist optimisation outputs as `saa_optimization_runs`.**
  Rejected for Phase 3 on YAGNI grounds (see Rationale).
  Reconsidered as additive Phase 5+ work if audit requirements
  surface.

### Charting architecture

- **Matplotlib for all web charts (rendered server-side as SVG).**
  Considered as a way to unify the rendering path with Shirley's
  Phase-5+ tool call. Rejected because (a) the loss of interactive
  hover, zoom, and click on standard charts is a real UX regression
  in the daily user flow, (b) matplotlib's print-DNA defaults look
  out of place in a web surface without substantial tuning, and
  (c) the visual consistency between standard charts and Shirley's
  dynamic charts can be achieved through CSS containment plus shared
  theme without forcing a single rendering engine.
- **Plotly for all charts including Shirley's dynamic outputs.**
  Rejected because Plotly cannot match matplotlib's open-ended
  graphical expressivity for arbitrary user-driven chart
  specifications. Constraining Shirley to Plotly-expressible charts
  would forfeit the killer-feature ambition (Shirley as
  general-purpose visualisation generator).
- **Hybrid where matplotlib figures are converted to Plotly via
  `mpl_to_plotly`.** Rejected as fragile (the converter is not
  reliable for complex figures) and as introducing a third
  rendering path that combines the failure modes of both.

### Cross-module API placement

- **Direct repository access from future consumers.** Rejected
  because it would couple consumers to repository internals (DTO
  shape, query patterns, transaction boundaries). The service layer
  is the appropriate stability seam.
- **Build the cross-module methods now in Phase 3.** Rejected on
  YAGNI grounds. Architectural preparation is structural; behavioural
  implementation waits for a real consumer.

## Consequences

### Positive

- Phase 3 has a tightly scoped, achievable goal with a clear
  acceptance test (visual identity in Sub-Strang 3d).
- The SAA web surface is end-to-end functional — read, write, visualise,
  optimise — without depending on the broader investment domain.
- The cross-module API on `SAAService` is structurally ready for the
  first real consumer (cash-flow projection) without forcing
  premature implementation.
- Plotly delivers interactive charts in the daily user flow without
  blocking the future Shirley dynamic-chart feature.
- The `chart-container` CSS convention plus shared `chart_theme.json`
  preserve visual consistency across rendering engines without
  technical entanglement.

### Negative

- The web SAA surface and the PyQt6 SAA widget are two separate
  data worlds during Phase 3. A user who configures an SAA in PyQt6
  cannot see it in the web (and vice versa). This is a deliberate
  consequence of ADR-0041 and is not a Phase-3 regression.
- Two rendering engines (Plotly in Phase 3, matplotlib in Phase 5+)
  must be kept theme-consistent. A regression test (planned for the
  Phase-5+ matplotlib introduction) will assert identical theme
  extraction; no such test exists in Phase 3.
- The cross-module API on `SAAService` is documented but not
  exercised in Phase 3. Until the first consumer arrives, the API
  contract is hypothetical and may need adjustment when reality
  strikes.

### Neutral / Follow-ups

- **Phase 4 ADR will record the investment-domain schema choice**
  (flat / per-type / hybrid) with the benefit of the migrated GUI
  as a concrete second consumer.
- **Phase 5+ ADR will record the Shirley dynamic-chart tool call**
  with sandboxing strategy, theme application hook, and result
  packaging.
- **Phase 5+ work will introduce the Plotly ↔ matplotlib theme
  consistency regression test** when the matplotlib path becomes
  active.
- **Excel-driven SAA calibration** (Phase 5 or 6 — automatic
  estimation of expected returns, volatilities, and correlations
  from historical investment data) is conceivable but is treated as
  a separate concern, not as a Phase-3 deferral. The data-supply
  story for PortfoliFLOW deserves its own ADR sequence outside the
  web-migration arc.

## Implementation Notes

- **New tables (Alembic migration b005, Sub-Strang 3b):**
  `asset_classes`, `saa_configurations`, `saa_asset_class_inputs`,
  `saa_correlations`. All four use `apply_tenant_rls(...)` and
  `audit_trigger_function`.
- **`is_active` enforcement:** partial unique index
  `CREATE UNIQUE INDEX uq_saa_configurations_active_per_tenant
   ON saa_configurations (tenant_id) WHERE is_active = TRUE;`
- **Repositories (Sub-Strang 3b):**
  `core/repositories/asset_class_repository.py`,
  `core/repositories/saa_configuration_repository.py`,
  `core/repositories/saa_asset_class_input_repository.py`,
  `core/repositories/saa_correlation_repository.py`.
- **Service (Sub-Strang 3b):** `services/saa/saa_service.py`. Three
  documented method groups (read workflows / write workflows /
  compute workflows). Cross-module API methods are documented in
  the module docstring but not implemented in Phase 3.
- **Chart spec module (Sub-Strang 3c):** `services/chart_specs/`
  with `base.py` (`layout_from_theme(theme, *, title, xlabel,
  ylabel) -> dict`) and `efficient_frontier.py`
  (`build_efficient_frontier_spec(...) -> dict`). Pure functions,
  no FastAPI dependency, importable by Shirley.
- **CSS convention (Sub-Strang 3c):**
  `web/static/css/components/charts.css` defines `.chart-container`
  with theme-aware background, border, padding, and minimum height.
  Both Plotly target divs and matplotlib `<img>` tags use this class.
- **Seed templates (Sub-Strang 3a or 3b — operator-decided):** the
  three PyQt6 seed templates (Conservative Multi-Strategy, Growth
  Private Markets, Balanced Institutional) are installed into the
  sentinel tenant by extending `portfoliflow bootstrap` (or by a
  separate `portfoliflow seed-saa` subcommand). Idempotent: re-runs
  do not duplicate. After bootstrap, the templates are ordinary
  configurations — fully editable, deletable, no special status.
- **No matplotlib code in Phase 3.** The Phase-3 web Python codebase
  must not import matplotlib. A regression guard is added to
  `tests/regression/` that asserts no module under `web/` or
  `services/chart_specs/` imports matplotlib. The PyQt6 codebase
  retains its matplotlib usage unchanged.
- **Tabulator.js for tabular surfaces:** asset-class input table,
  correlation matrix, and optimisation weights table all use
  Tabulator with the save-pattern (client-side staging, explicit
  Save button). Cross-field validation lives in `SAAService`, not
  in JavaScript.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes:** *Modularity* (cross-module API
  surface on `SAAService`), *Maintainability* (deferred Phase-4
  schema avoids premature commitment), *Modifiability* (matplotlib
  reserve preserves Shirley's future feature without locking the
  Phase-3 path).
- **BAIT AT 7.2 / VAIT:** the SAA configuration is the basis of
  future allocation-limit compliance checks (Phase 5+ cash-flow
  projection). The `is_active` flag plus audit-logged changes
  provide a clear "which configuration was authoritative on date X"
  query path for auditors.
- **Audit evidence:**
  - Schema: `db/migrations/versions/b005_*.py` defines the four
    tables with RLS and audit triggers.
  - Tenant isolation: `tests/regression/test_rls_schema_invariants.py`
    asserts all four new tables have `relrowsecurity` and
    `relforcerowsecurity` set, plus at least one policy.
  - Audit trail: `audit_log` rows for any SAA write include
    `tenant_id` and `user_id` (verified in
    `tests/repositories/test_tenant_context_user_id.py`'s analogue
    for SAA tables).
  - Charting separation: `tests/regression/test_no_matplotlib_in_web.py`
    asserts no Phase-3 web code imports matplotlib.

## References

- ADR-0033: Web Migration — PyQt6 Desktop to FastAPI Web
- ADR-0034: Persistence Backend — Postgres for Multi-Tenant Operation
- ADR-0035: Multi-Tenant Architecture — Tenant Isolation via RLS
- ADR-0037: Frontend Stack — FastAPI/Jinja/HTMX/SSR Default
- ADR-0039: Migration Pattern — Strangler with Tagged Demo-Stable Branch
- ADR-0040: Sentinel Bootstrap — CLI-Driven Idempotent Initialization
- ADR-0041: Persistence Entry-Points — Strangler-Coexistence
- `gui/widgets/saa_widget.py` (PyQt6 reference implementation)
- `modules/back_office/saa.py` (PyQt6 module wrapper)
- `analytics/portfolio_optimizer.py` (shared analytics engine)
- Plotly.js documentation: https://plotly.com/javascript/
- Tabulator.js documentation: https://tabulator.info/
- matplotlib backend reference: https://matplotlib.org/stable/users/explain/figure/backends.html

---

## Revision History

| Date       | Author                       | Change                                    |
|------------|------------------------------|-------------------------------------------|
| 2026-05-05 | PortfoliFLOW project owner   | Initial draft, Status: Accepted           |
| 2026-05-05 | PortfoliFLOW project owner   | Sub-Strang 3b complete: migration b005, ORM models, repositories, SAAService, seed templates installed |
| 2026-05-06 | PortfoliFLOW project owner   | Sub-Strang 3c complete: read-only SAA web surface (list, detail, optimisation HTMX partial), `services/chart_specs/` + `chart-container` CSS, Plotly + Tabulator vendored. Test count: 775 (+36) |
| 2026-05-06 | PortfoliFLOW project owner   | Sub-Strang 3d complete: write surface (POST /saa, PUT /saa/{id}/save, POST /saa/{id}/activate, DELETE /saa/{id}, asset-class CRUD page), Tabulator inline editing on inputs + correlations with mirror, save-pattern with dirty-state and beforeunload, audit-trail web tests, cross-tenant write-isolation tests. Phase-3 acceptance report at `docs/phase-3-acceptance-report.md`. Numerical MVO identity verified by construction (shared `analytics/portfolio_optimizer.py`). Test count: 802 (+27). Phase 3 functionally complete; visual sign-off pending. |
| 2026-05-06 | PortfoliFLOW project owner   | Audit note: `SAAService.get_active_configuration()` was implemented eagerly during Sub-Strang 3b (delegates to `SAAConfigurationRepository.get_active()`). The other two cross-module methods named in §3 (`get_constraints_for_compliance_check()`, `get_configuration_by_name()`) remain unimplemented as planned. The eager implementation is additive and does not change the §3 deferral posture; documented here for traceability. |
