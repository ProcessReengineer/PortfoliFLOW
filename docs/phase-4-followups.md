# Phase 4 Follow-ups for Phase 5+

This document tracks items that surfaced during Phase 4 but are
deliberately deferred to Phase 5 or later, or that record post-hoc
observations about Phase-4 commits that are intentionally not
rewritten.

These are not bugs blocking Phase-4 acceptance; they are conscious
scope choices and documentation of items that should be considered
when Phase 5 work begins.

---

## P5-1 — Sektor- and Country-Breakdown Normalisation

**Source:** ADR-0043 §1; Phase-4 plan §3.

The `Attributes` sheet of the V2 Excel format carries per-investment
sector and country split rows that Phase 4 leaves unparsed. The
splits remain in the `data_upload_sheets` JSONB substrate — recoverable
but not normalised.

Phase 5, alongside the Charts/Statistics web migration, will introduce
two new tables (`investment_sector_weights` and
`investment_country_weights`) and extend
`InvestmentExtractor` to populate them. The existing
`_validate_split_payload` helper in
`services/data_normalization/investment_extractor.py` is the natural
extension point.

## P5-2 — Type-specific Analytics

**Source:** ADR-0043 §2; Phase-4 plan §3.

Phase 4 treats all seven investment types uniformly (PE-flavoured
TVPI / DPI / IRR for Listed Equity is semantically dubious but
deliberate). A post-Phase-5 re-kickoff will revisit per-type
analytics:

- Listed-Bond duration / yield-to-maturity calculations.
- Real-Estate gutachten-driven valuation conventions.
- Private-Debt commitment-vs-drawn semantics.

Implementation pathway is additive (per-type side tables off
`investments` keyed by `(investment_id, investment_type)`) rather
than a `investments`-table refactor.

## P5-3 — Plan Versioning

**Source:** ADR-0043 §1; Phase-4 plan §3.

Plan series are currently overwritable: a new plan replaces the old
plan in place. Plan history is reconstructable via the `audit_log`
JSONB payload, which is acceptable for the rare reconstruction case
but not ergonomic for operational queries.

If a Phase-5+ consumer asks "what was the plan the manager submitted
in early 2024 vs the updated plan in mid-2025?", introduce a
`plan_revision` integer column on `investment_navs` and
`investment_cashflows`, defaulting to 1, with an additive UNIQUE
extension `(investment_id, as_of_date, nav_kind, plan_revision)`.

## P5-4 — Currency Stammtabelle and FX Handling

**Source:** ADR-0043 §2; Phase-4 plan §3.

`currency` is currently a free-form `TEXT` column with an ISO-4217
convention. Phase 5+ FX work will introduce a `currencies` table and
a daily-FX-rate substrate; `investments.currency` becomes a FK at
that point. Migration path is straightforward (one ALTER TABLE +
backfill from existing free-form values).

## P5-5 — Multi-Asset-Class Weights

**Source:** ADR-0043 §2; Phase-4 plan §3.

Investments are linked 1:1 to asset classes. Multi-strategy funds
are mapped by operational convention to a single dominant class.
If a future use-case requires M:N (e.g. weighted breakdown), introduce
an `investment_asset_class_weights` table with a default weight=1.0
backfill per existing investment, and either deprecate
`investments.asset_class_id` or repurpose it as the
"primary class" pointer.

---

## P4-Hygiene-1 — `ImportError` Name Shadowing in `data_normalization`

**Source:** Phase-4 cleanup pass.

`services/data_normalization/__init__.py` re-exports a dataclass
named `ImportError`, which **shadows the Python builtin** within the
module's namespace. The dataclass docstring acknowledges the
shadowing (`investment_extractor.py` line 296-ish), and `__all__`
includes it deliberately — it is a structured row-level error record,
not an exception (per the ADR-0043 §3 partial-success convention).

The shadowing is a hygiene smell rather than a defect: any code
doing `from services.data_normalization import *` would see the
builtin overshadowed. No current consumer does so. A future rename
to `ImportRowError` (or similar) is a Phase-5 candidate when the
data-normalisation surface grows beyond the V2 extractor and the
cost of the rename stays small.

## P4-Hygiene-2 — Phase-4 Commit-Message Hygiene Notes

**Source:** Phase-4 cleanup pass; ADR-0014 (Conventional Commits).

The Phase-4 commit log carries two minor irregularities that are
**deliberately not retroactively rewritten** (project policy: no
`git rebase` of accepted history without explicit owner approval).
They are recorded here so future commit-discipline reviews can
treat them as known precedents rather than rediscover them:

1. **`2ee91b4 refactor: Phase 4 step 1`** — bundles the entire
   Sub-Strang 4a payload (b006 migration + 3 ORM models +
   3 repositories + InvestmentService skeleton + bootstrap CLI
   extension + ADR-0043 + ~46 tests, 27 files / 5682 insertions
   in one commit). Conventional Commits would have preferred ~5
   smaller commits (one per logical unit) and a more descriptive
   subject (`feat(schema): add investment domain — migration,
   ORM, repos, service skeleton, bootstrap`). Type `refactor`
   is also a misnomer: this was net-new feature work.

2. **`93bb78d feat(web): add investment NAV timeseries Plotly spec`** —
   subject scope reads `web` but the diff lives entirely under
   `services/chart_specs/`. The body correctly describes the file
   placement; only the scope token is misleading. A correct subject
   would have been `feat(services): add investment NAV timeseries
   Plotly spec` or `feat(charts): ...`.

Both are surfaced to the commit-discipline conscience for Phase 5+
without a rebase. The 4b/4c commit logs are clean; the 4d
consolidation commits use accurate scopes.

---

## Out-of-Scope (Recorded Elsewhere)

These items are mentioned for completeness; their authoritative
records live in other documents. Do not add a Phase-5 tracking line
here for them — their ADRs are the source of truth.

- **GUI migration onto Postgres** — phase-4-plan.md §3, ADR-0033.
  Phase 5+ scope.
- **Charts / Statistics web surfaces** — phase-4-plan.md §3,
  ADR-0033. Phase 5+ scope.
- **SAA-Cashflow cross-module integration** — phase-4-plan.md §3.
  Pending Phase-5 cashflow forecasting.
- **Import-format specification extension for fields not yet supported** —
  phase-4-plan.md §3. Tracked when a field need surfaces.
