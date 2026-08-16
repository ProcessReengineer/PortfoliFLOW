# ADR-0043: Investment Domain Schema and Excel Transformation Pathway

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, domain-schema, persistence, excel-import, architecture

---

## Context

Phase 4 of the PyQt6 → Web migration introduces the **investment-domain
schema** that ADR-0042 §1 deliberately deferred from Phase 3. With Phase 3
complete (SAA module migrated, asset-class catalogue per tenant, four
SAA-specific tables in operation), Phase 4 has the concrete second
consumer — the Excel-driven investment-tracking workflow that p&p
operates today — that lets the schema choice be made against real
domain pressure rather than design-without-consumer.

Two scope decisions had to be made before Phase 4 implementation could
begin, and the second is a direct consequence of the first:

1. **Schema form for investments.** A flat polymorphic table with a
   type-discriminator, a table-per-investment-type variant, or a
   common-base-plus-side-tables hybrid. The choice depends on whether
   Phase 4 models type-specific behaviour (which forces side-tables or
   per-type tables) or treats all types uniformly (which makes a flat
   table the appropriate response to YAGNI discipline).

2. **Excel-import-to-investment-schema convergence path.** ADR-0041 §3
   deferred the convergence to Phase 4. Phase 4 now decides whether the
   Phase-2 JSONB substrate (`data_uploads` / `data_upload_sheets`) is
   replaced, augmented by a parallel write path, or kept as immutable
   audit substrate behind an asynchronous transformation step.

Both decisions are scope-shaping rather than security-critical, but
both have audit-relevant downstream effects: the schema decision
determines what data lives in Postgres at the end of Phase 4, and the
import-path decision determines the trajectory of Phase 5+ data quality
work (Charts/Statistics migration, Portfolio-Review reporting).

This ADR records both decisions together because they share the same
underlying force — *deliberate restraint about type-specific modelling
during a multi-phase migration*.

## Decision

### 1. Investment-domain schema in Phase 4

Phase 4 introduces a **flat polymorphic** investment-domain schema with
three tenant-scoped tables, all RLS-protected via the standard
`apply_tenant_rls(...)` helper, all audit-logged via the
`audit_trigger_function` from b001:

- **`investments`** — one row per investment instrument, with an
  `investment_type` discriminator column (TEXT with CHECK constraint on
  seven allowed values: `private_equity`, `private_debt`, `real_estate`,
  `infra_equity`, `listed_equity`, `listed_bonds`, `other`). All seven
  types share the same column structure in Phase 4. A
  `type_specific_data` JSONB column is reserved as an emergency exit
  for Phase 5+ extensions but is not used in Phase 4.
- **`investment_navs`** — date-stamped valuations per investment, with
  `nav_kind` discriminator (`plan` | `actual`). Plan and actual
  series are stored in parallel; neither is overwritten when the other
  changes. `as_of_date` is a `DATE` (statement-day semantics, not
  point-in-time).
- **`investment_cashflows`** — cashflow events per investment, with
  `flow_type` discriminator (seven values: `capital_call`,
  `distribution`, `fee`, `carry`, `dividend`, `coupon`, `other`) and
  `flow_kind` discriminator (`plan` | `actual`). `flow_timestamp` is a
  `TIMESTAMPTZ` with a default convention of 12:00 UTC when the precise
  time is unknown.

Investments are linked to the asset-class catalogue (Phase 3) via a
**1:1 foreign key** (`investments.asset_class_id`). Multi-strategy
funds are mapped to a single asset class by operational convention at
p&p; the rare multi-asset-class investment is treated as a
single-class assignment with the dominant class winning.

Plan series are **overwritable**: a new plan replaces the old plan in
place. Plan history is reconstructable from the `audit_log` JSONB
payload of the relevant `UPDATE` event. Plan versioning as a
first-class schema feature is deferred — when the operational need
materialises, an additive `plan_revision` column or sibling table can
be introduced without retrofit.

A bootstrap-installed **"Unclassified" asset class** per tenant serves
as a fallback target for Excel imports whose `Asset Class` field is
empty. In ordinary operation at p&p the field is always populated and
this fallback is dormant; it exists as a safety net for partial
imports and edge cases.

### 2. No type-specific modelling in Phase 4

All seven investment types are treated uniformly throughout Phase 4:

- The `investments` table has no per-type sub-table.
- The analytics layer (TVPI, DPI, IRR, NAV time series) is computed
  identically for all types. Listed Equity and Cash will get
  PE-flavoured TVPI and DPI even where it is semantically dubious.
- The web CRUD surface (sub-stream 4b) presents a uniform form per
  investment type. Type-specific fields, validation rules, or display
  conventions are not introduced.

Type-specific behaviour (per-type charts, per-type validation, per-type
analytics) is reserved for a future re-kickoff after Phase 5.
The reasoning: type-specific work requires deep domain expertise per
type (Listed-Bond yield calculations differ structurally from PE
J-curves; Real Estate valuation conventions are gutachter-driven, not
manager-reported), and that depth does not belong in a refactoring
phase. The flat schema preserves the optionality to add type-specific
side tables additively in Phase 5+ without restructuring existing data.

### 3. Excel-import-to-investment-schema transformation

Phase 4 commits to the **asynchronous transformation** model (B1):

- The Phase-2 Excel-upload pathway (`data_uploads` /
  `data_upload_sheets`) remains unchanged. Excel uploads continue to
  persist a JSONB snapshot per upload, validated structurally against
  the V2 spec (ADR-0009). The JSONB substrate is **immutable audit
  evidence** of what was uploaded.
- A new endpoint
  `POST /api/data-uploads/{upload_id}/import-as-investments` reads the
  JSONB snapshot, validates the contents against the investment-domain
  schema, and writes the normalised representation into `investments`,
  `investment_navs`, and `investment_cashflows`. The transformation is
  triggered explicitly by the user via a UI button on the uploads list.
- The transformation returns a structured `ImportResult` reporting
  per-investment success, partial success, or failure. Validation
  errors do not abort the entire transformation; failed rows are
  reported, succeeded rows are committed.
- The transformation is **idempotent**. Repeated invocation against the
  same upload re-runs the transformation and produces the same final
  state in the investment tables. Investment identity is resolved via
  the natural key `(tenant_id, name)`.

The transformation logic is **replace-by-investment** (B1.1): for each
investment in the Excel file, all existing NAVs and cashflows of that
investment are deleted and re-inserted. This treats the Excel file as
the authoritative source — manual edits made in the system between
imports are deliberately overwritten on the assumption that the Excel
file is corrected first and the import is then re-run.

Investments present in the system but **absent** from the Excel file
are subjected to **soft-delete with automatic reactivation** (B2.b):

- An investment in the system but not in the latest Excel import is
  set to `is_active = FALSE`.
- An investment previously soft-deleted that reappears in a subsequent
  Excel import is automatically reactivated (`is_active = TRUE`).

This is symmetric, predictable, and operates at the tenant level (the
Excel file is treated as the complete portfolio view per tenant). The
soft-delete preserves audit trail and historic NAV/Cashflow data while
removing the investment from active reporting.

### 4. Cross-module API extension to Phase 3

Sub-stream 4c requires resolving asset-class names from Excel
(`Attributes` sheet, `Asset Class` row) to `asset_class_id` UUIDs.
This requires a method on `AssetClassRepository`:

```python
async def get_by_code(self, code: str) -> AssetClassDTO | None:
    """Resolve an asset class by its tenant-scoped code, or None."""
```

This method was not implemented in Phase 3 (no concrete consumer).
It is implemented eagerly in Phase 4 because the Excel-import path is
that concrete consumer. Per ADR-0042 §3 cross-module API discipline:
methods are added when a real consumer requires them, not on
speculation.

### 5. Web CRUD surface as schema validation

The Phase-4 web CRUD surface for investments (sub-stream 4b) serves
two functions:

- **Schema validation through use.** Building CRUD operations against
  the new schema is the most direct test of whether the schema fits
  real workflows. If creating, listing, editing, and deleting
  investments is awkward against the chosen schema, the schema is
  reconsidered before sub-stream 4c writes the more complex
  Excel-transformation surface against it.
- **Debug access for Excel-import maintenance.** Once Excel imports
  are operational, individual data points (a single corrected
  cashflow, a single re-stated NAV) can be edited directly through
  the web surface without round-tripping through Excel.

The CRUD surface is not the primary user-facing entry point at p&p —
Excel-import is. The CRUD surface is a developer-and-maintenance
surface in Phase 4.

## Rationale

### Why a flat polymorphic schema

- **No type-specific behaviour to model in Phase 4.** Seven investment
  types share one analytics path, one chart structure, one form
  structure. Side tables would be empty or near-empty.
- **YAGNI discipline.** Phase 3 explicitly deferred the schema choice
  until a concrete second consumer was available. The consumer
  (Excel-import workflow plus PE-flavoured analytics) does not
  distinguish between types in Phase 4.
- **Migration to a more typed schema is additive.** When Phase 5+ work
  introduces type-specific features (e.g. Listed-Bond duration
  calculation, Real Estate gutachten-based valuation), additive side
  tables can be introduced without restructuring `investments`. The
  `type_specific_data` JSONB column is a temporary parking lot for
  the transitional period.
- **Cross-type queries are common.** Use-Case (b) — "haben wir noch
  Platz für Investment X?" [English: "do we still have room for
  investment X?"] — aggregates across all types in a tenant.
  A flat schema serves this without UNION joins.

### Why immutable JSONB substrate plus async transformation

- **Audit-trail preservation.** The Phase-2 JSONB substrate is the
  source-of-truth for "what was uploaded". Replacing or bypassing it
  would weaken the audit trail.
- **Resilience to partial Excel quality.** Async transformation
  separates the upload (always succeeds if the file is structurally
  valid V2) from the normalisation (may surface per-row errors). This
  matches the operational reality that Excel files come from external
  sources and are not always clean.
- **Re-importability.** A previously uploaded Excel file can be
  re-transformed after a logic fix or asset-class update without
  re-uploading the file.
- **Strangler-pattern consistency.** ADR-0041 §3 designated Phase 4 as
  the convergence point. Async transformation honours the substrate
  Phase 2 built rather than discarding it.

### Why replace-by-investment over merge-by-natural-key

- **Excel is the authoritative source.** The project owner confirmed
  in the scoping discussion: "wenn etwas bei einem Excel korrigiert wurde,
  liegt das in der Verantwortung des Nutzers — vermutlich handelt es
  sich um die Korrektur eines fehlerhaften Wertes." [English: "if
  something has been corrected in an Excel file, that lies in the
  user's responsibility — presumably it concerns the correction of
  an erroneous value."] Manual system edits between imports are the
  exception, not the norm.
- **Predictability.** Replace-by-investment means the post-import
  state of an investment is entirely determined by the Excel content.
  Merge-by-natural-key creates a hybrid state that is harder to reason
  about and harder to debug.
- **Audit log preserves overwritten data.** The
  `audit_trigger_function` captures every DELETE and INSERT, so any
  overwritten value is reconstructable from `audit_log.old_data` if
  needed.

### Why soft-delete with automatic reactivation

- **Excel as complete portfolio view.** The project owner confirmed
  that the Excel file always contains all investments for a tenant. An
  investment missing from a fresh Excel upload is intended to be
  inactive.
- **Symmetric reactivation handles re-introduction.** If an investment
  is removed from one Excel revision but reappears in the next, the
  user does not have to manually reactivate it. The system treats the
  current Excel content as the authoritative active set.
- **Soft-delete preserves history.** Deactivated investments retain
  their NAVs, cashflows, and audit trail. Hard delete would lose
  historical reporting capability for portfolio-level questions like
  "what was our PE allocation in Q3 of last year?".

## Alternatives Considered

### Schema form

- **Table-per-investment-type.** Rejected. Seven tables in Phase 4
  would be empty-shell tables (no type-specific columns to populate),
  generate seven repository classes that share the same code, and
  force every cross-type query to be a UNION. The added structure has
  no informational content in Phase 4.
- **Common-base-plus-side-tables.** Rejected for Phase 4. With no
  type-specific fields to model, the side tables would be empty.
  Reserved as a Phase-5+ option if type-specific fields surface
  during charts/statistics migration or the post-Phase-5 re-kickoff.

### Excel-import-to-schema convergence

- **Dual-write at upload time (B2).** Rejected. Forces the upload
  endpoint to validate against the investment schema synchronously,
  making the upload less robust against partial-quality Excel files.
  Also creates two write paths to maintain (JSONB and normalised) per
  upload, doubling the failure surface.
- **Replacement of the JSONB substrate (B3).** Rejected. The JSONB
  substrate is immutable audit evidence per ADR-0041 §3. Discarding
  it would break the strangler-pattern guarantee and weaken the
  audit trail.

### Update semantics on re-import

- **Merge-by-natural-key (B1.2).** Rejected. Creates hybrid state
  where some data points reflect the Excel file and some reflect
  prior manual edits, which is hard to reason about and hard to
  audit.
- **Append-only (B1.3).** Rejected. Would accumulate duplicate
  cashflows and NAVs on every re-import, requiring an explicit
  deduplication step that itself has the same semantic complexity as
  the merge-by-natural-key path.

### Soft-delete semantics

- **Investments-not-in-Excel remain unchanged (B2.a).** Rejected.
  Inconsistent with the operational convention that the Excel file
  is the complete portfolio view per tenant.
- **Hard-delete investments-not-in-Excel (B2.c).** Rejected. Loses
  historical NAV and cashflow data, breaking historical reporting.

### Plan history

- **First-class plan versioning with a `plan_revision` column.**
  Rejected for Phase 4. No concrete consumer asks "what was the plan
  the manager submitted in early 2024 vs the updated plan in
  mid-2025?" yet. Audit log preserves overwritten plan data; that is
  sufficient for the rare reconstruction case. Additive introduction
  of plan versioning is straightforward when a real consumer
  surfaces.

### Multi-asset-class assignment per investment

- **M:N investment-to-asset-class mapping with weights.** Rejected
  for Phase 4. The project owner confirmed that p&p investments are
  mapped 1:1 to asset classes by operational convention. Multi-strategy funds
  are the exception and are assigned to a single dominant class.
  Migration to M:N is additive (new `investment_asset_class_weights`
  table, default weight=1.0 per existing investment, deprecate the
  1:1 column or repurpose as primary-class).

### Currency representation

- **Currency stammtabelle (FK).** Rejected for Phase 4. Five or so
  ISO 4217 codes cover real p&p operations. A `currencies` table is
  Phase 5+ work tied to FX-rate handling.

## Consequences

### Positive

- The investment-domain schema is shaped by a real second consumer
  (Excel-import workflow plus CRUD surface), not designed in vacuum.
- Type-specific work is preserved as future optionality without
  forcing premature commitment in Phase 4.
- The Phase-2 JSONB substrate continues to serve as audit evidence
  and re-importability anchor without requiring a parallel-write
  burden.
- Replace-by-investment plus soft-delete-with-reactivation is a
  predictable, idempotent operational model.
- Cross-module API extension (`AssetClassRepository.get_by_code()`)
  follows Phase-3 disciplinary precedent: eager when a real consumer
  is present, deferred otherwise.

### Negative

- All seven investment types share one analytics path in Phase 4,
  including types where the analytics are semantically dubious
  (Listed Equity TVPI/DPI). This is documented PE-flavoured uniformity
  that will need to be revisited in the post-Phase-5 re-kickoff.
- Manual edits in the web CRUD surface are subject to overwrite by
  the next Excel import. This is operationally acceptable per the
  project owner's confirmation but creates a "do not edit between
  imports" convention that must be communicated.
- The `type_specific_data` JSONB column is a deliberate optionality
  reserve. Without disciplined use, it could become a Schema-Drift
  vector. The convention in Phase 4 is: not used; in Phase 5+: only
  for transitional storage; the post-Phase-5 re-kickoff decides
  per-type which fields are promoted to columns or sub-tables.
- The Strangler asymmetry is **deepened** in Phase 4. Investments
  added in the web are invisible to the GUI. A demo running across
  both surfaces requires careful coordination (see
  `demo-stability-checklist.md`).

### Neutral / Follow-ups

- A post-Phase-5 re-kickoff for type-specific functionality is
  expected. That kickoff will decide what fields per investment type
  are first-class columns, what becomes side tables, and what
  analytics are type-aware vs uniform.
- Sektor- and Country-Breakdown normalisation (currently in
  `Attributes` sheet, partitioned at runtime) is reserved for Phase 5
  alongside Charts/Statistics migration.
- Plan-versioning, currency stammtabelle, and M:N
  investment-to-asset-class are all additive Phase-5+ extensions
  available when real consumers surface.

## Implementation Notes

### Migration b006

Single Alembic migration creates all three tables with RLS, audit
triggers, indices, CHECK and UNIQUE constraints in dependency order.

### ORM models

Three new files under `core/models/`: `investment.py`,
`investment_nav.py`, `investment_cashflow.py`. Each follows the
Phase-3 pattern: declarative SQLAlchemy mapping with Postgres-aware
types (`PG_UUID`, `JSONB`, `DateTime(timezone=True)`).

### Repositories

Three new files under `core/repositories/`: `investment_repository.py`,
`investment_nav_repository.py`, `investment_cashflow_repository.py`.
Each uses the DTO pattern from Phase 3 and acquires sessions via
`tenant_context()`.

### Service layer

`services/investments/investment_service.py` aggregates the three
repositories. Documented method groups:
- **Read workflows.** Routes consume aggregate read DTOs (e.g.
  `InvestmentDetailDTO` with NAV history and cashflows).
- **Write workflows.** Atomic create / update / delete operations.

Cross-module API methods are explicitly **not** implemented in Phase 4
unless a concrete consumer exists in another Phase-4 sub-strang.

### Bootstrap extension

`portfoliflow bootstrap` (CLI) is extended to install an
"Unclassified" asset class per tenant, idempotent. This is additive to
the Phase-3 bootstrap behaviour.

### Excel-transformation service

`services/data_normalization/investment_extractor.py` reads the JSONB
snapshot from `data_upload_sheets`, applies the V2-to-Investment
mapping, and produces a structured `InvestmentExtractionResult`. The
service has no FastAPI dependency and is unit-testable in isolation.

### Audit-trail evidence

For each new table:
- An entry in `tests/regression/test_rls_schema_invariants.py` (the
  test scans dynamically; verification only).
- A cross-tenant isolation test in
  `tests/repositories/test_<table>_audit_and_isolation.py`.
- A web-side audit test in
  `tests/web/test_investments_audit_trail.py` (and analogues for
  NAVs, cashflows).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes:** *Modularity* (per-table
  repositories with DTO contract), *Maintainability* (deferred
  type-specific modelling avoids premature commitment),
  *Modifiability* (additive evolution path to type-aware schema in
  Phase 5+).
- **BAIT AT 7.2 / VAIT:** Investment NAV history with plan/actual
  separation provides a clear "which valuation was authoritative on
  date X" audit trail. The audit log captures every overwrite.
  Replace-by-investment with audit evidence preserves the chain of
  custody for re-imported data.
- **Audit evidence:**
  - Schema: `db/migrations/versions/b006_*.py`.
  - Tenant isolation:
    `tests/regression/test_rls_schema_invariants.py` (dynamic).
  - Audit trail: `audit_log` rows for any investment write include
    `tenant_id` and `user_id` (verified in
    `tests/repositories/test_investment_audit_and_isolation.py`).
  - Excel-import idempotence:
    `tests/services/test_investment_extractor.py` covers
    round-trip, replace, soft-delete-with-reactivation,
    asset-class-fallback, cross-tenant isolation.

## References

- ADR-0033: Web Migration — PyQt6 Desktop to FastAPI Web
- ADR-0034: Persistence Backend — Postgres for Multi-Tenant Operation
- ADR-0035: Multi-Tenant Architecture — Tenant Isolation via RLS
- ADR-0037: Frontend Stack — FastAPI/Jinja/HTMX/SSR Default
- ADR-0039: Migration Pattern — Strangler with Tagged Demo-Stable Branch
- ADR-0040: Sentinel Bootstrap — CLI-Driven Idempotent Initialization
- ADR-0041: Persistence Entry-Points — Strangler-Coexistence
- ADR-0042: Phase 3 Scope — SAA-Only Domain Schema and Plotly-First Charting
- ADR-0009: Excel V2 Multi-Sheet Import Format with Dynamic Column Discovery
- `services/saa/saa_service.py` (Phase-3 reference for service-layer
  cross-module API discipline)

---

## Revision History

| Date       | Author                       | Change                                    |
|------------|------------------------------|-------------------------------------------|
| 2026-05-06 | PortfoliFLOW project owner   | Initial draft, Status: Accepted           |
| 2026-05-06 | PortfoliFLOW project owner   | Sub-stream 4a complete: migration `b006_add_investment_domain.py` (the three tables with RLS, `FORCE ROW LEVEL SECURITY`, audit triggers, indices, CHECK and UNIQUE constraints), three ORM models (`Investment`, `InvestmentNav`, `InvestmentCashflow`), three repositories (`InvestmentRepository`, `InvestmentNavRepository`, `InvestmentCashflowRepository`), `InvestmentService` skeleton with read/write methods, and `cli/bootstrap.py` extension installing the per-tenant `unclassified` asset class idempotently. Test coverage: ~46 new tests across `tests/repositories/test_investment_*.py`, `tests/services/test_investment_service.py`, plus 3 schema-regression-guard parameterisations and the CLI bootstrap-seed tests. |
| 2026-05-06 | PortfoliFLOW project owner   | Sub-stream 4b complete: investment CRUD web surface (`web/routes/investments.py`, `web/templates/investments/`, `services/chart_specs/investment_nav_timeseries.py`) plus audit-trail and cross-tenant write-isolation tests. `InvestmentService` extended with `get_investment` / `get_nav` / `get_cashflow` for routing-time 404 guards (additive; non-breaking); `InvestmentCashflowRepository.get_by_id` added for the same reason. Test coverage: 49 web tests across `tests/web/test_investments_routes.py`, `test_investments_write_routes.py`, `test_investments_audit_trail.py`, plus 11 chart-spec tests in `tests/services/test_chart_specs_investment_nav_timeseries.py`. Schema unchanged. |
| 2026-05-06 | PortfoliFLOW project owner   | Sub-stream 4c complete: Excel-import-to-investments transformation. `services/data_normalization/investment_extractor.py` parses Phase-2 JSONB snapshots into typed `ImportedInvestment` / `ImportedNav` / `ImportedCashflow` dataclasses with strict cashflow-sign validation, German/English Investment-Type alias normalisation, and `unclassified` asset-class fallback. `InvestmentService.transform_upload_to_investments` orchestrates replace-by-investment plus soft-delete-with-reactivation in a single tenant-scoped transaction; row-level errors surface as `InvestmentExtractionResult.errors` per the partial-success convention. Cross-module API per §4: `AssetClassRepository.get_by_code` upgraded to case-insensitive lookup (Excel inputs are inconsistent in case). Web surface: `POST /api/data-uploads/{upload_id}/import-as-investments` (CSRF-protected, supports `?dry_run=true` for the UI confirm-before-write preview); the existing data-import detail template gains an "In Investments importieren" [English: "Import into investments"] workflow with preview block (`web/static/js/data_import_detail.js`). Test coverage: 15 unit tests for the extractor (`tests/services/test_investment_extractor.py`), 7 integration tests for the service transform (`tests/services/test_investment_service_transform.py`), 4 web-route tests (`tests/web/test_investments_import_routes.py`), plus an extended cross-tenant case-insensitive `get_by_code` test (`tests/repositories/test_asset_class_repository.py`). Schema unchanged. |
| 2026-05-06 | PortfoliFLOW project owner   | Sub-stream 4d complete (Phase-4 consolidation, no functional changes). ADR-0043 confirmed in the `docs/adr/README.md` index; the post-table narrative updated to reflect Phase 4 functionally complete pending acceptance sign-off. Schema-regression guard (`tests/regression/test_rls_schema_invariants.py`) verified green for all three Phase-4 tables (RLS + `FORCE ROW LEVEL SECURITY` + ≥1 policy on `investments`, `investment_navs`, `investment_cashflows`). Repo cleanup pass surfaced no TODO / FIXME / debug-prints / shim-imports in the Phase-4 surface; the only minor note is the documented intentional `ImportError` name shadowing in `services/data_normalization/__init__.py` (recorded in `docs/phase-4-followups.md`). Test-coverage gap analysis filled two substantive gaps with verification value: the bootstrap-fault path on `transform_upload_to_investments` (missing `unclassified` → loud `ValueError`, IT-08) and the structural-400 translation path on the import route (`ImportFormatError` → 400, IRT-05). Conventional-Commits review: 4b commits clean, 4a was bundled into a single oversized commit (`2ee91b4 refactor: Phase 4 step 1`) and the chart-spec commit `93bb78d` carries a misleading `feat(web)` scope for `services/chart_specs/`-only changes; both are recorded in `docs/phase-4-followups.md` and not retroactively rebased per project policy. CLAUDE.md glossary extended with seven Phase-4 domain terms (`Investment`, `Investment Type`, `NAV`, `Cashflow`, `nav_kind`, `flow_kind`, `flow_type`) plus a "Phase 4" entry in the Current project status block. Final test count: 939 passing (937 baseline + 2 gap tests added in 4d), 2 skipped. |
| 2026-05-06 | PortfoliFLOW project owner   | Sub-stream 4e complete: `docs/phase-4-acceptance-report.md` written as the final Phase-4 sign-off document. The 13-section report (Summary, Scope Recap, Schema-Structural Verification, Web-CRUD-Surface Verification, Excel-Import-Surface Verification, Use-Case Acceptance Tests for Use-Cases A/B/C, Performance Sanity Check, Strangler Asymmetry / Demo Discipline, ER Diagram, Phase-4 Omissions, Browser Walkthrough, Risk Notes, Sign-off) verifies the §1–§7 acceptance points from `docs/phase-4-acceptance-criteria.md`. Concrete demonstrations: Use-Case A walked through synthetic Permira VII plan/actual cashflow parallelism with the SQL aggregation query; Use-Case B walked through the planned-capital-calls-per-asset-class roll-up SQL against the Phase-4 schema; Use-Case C produced TVPI = 1.4167 x, DPI = 0.2500 x, IRR = 10.55 % p.a. for the actual current state and TVPI = 2.0000 x, DPI = 2.0000 x, IRR = 12.98 % p.a. for the plan end-of-life state, each computed via the existing `services.reporting.data_providers._calculations.compute_irr` engine without modification. Performance sanity check: at 100 investments × 20 NAVs × 50 cashflows the `/investments` list rendered in 5–14 ms (mean 6.3 ms) and `/investments/{id}` detail rendered in 5–21 ms (mean 7.5 ms), comfortably below the 200 ms / 300 ms thresholds; no index tuning required. The full pytest suite is 939 passing / 2 skipped (+135 net new Phase-4 tests vs. the `phase-3-complete` head `c0970c4` baseline of 804). Schema unchanged. Acceptance is contingent on the project owner walking the §11 browser checklist; once confirmed, `phase-4-complete` will be tagged on `web-migration`. |
| 2026-05-10 | PortfoliFLOW project owner   | Translated residual German passages to English per ADR-0008 (Phase-6 Block 0c) and anonymised author attributions per ADR-0008 follow-up. The two verbatim project-owner quotes in §Rationale are preserved with English glosses appended in square brackets; the German UI-label string `"In Investments importieren"` is preserved in the sub-stream-4c Revision-History row with an English gloss appended. No substantive change; status, decisions, and content unchanged. |
