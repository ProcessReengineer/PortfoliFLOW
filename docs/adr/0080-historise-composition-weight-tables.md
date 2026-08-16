# ADR-0080: Historise the Composition-Weight Tables (sector / region / country)

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** N/A (follow-up to ADR-0079 §2; trigger: composition-drift accuracy for the Total-Return surface and reproducible reporting-date snapshots)
- **Tags:** persistence, schema-migration, time-series, composition-weights, historisation, analytics, multi-tenancy

---

## Context

ADR-0079 §2 introduced two **time-series** per-investment weight tables for
the Fixed-Income archetype — `investment_rating_weight` and
`investment_maturity_weight` — each keyed on `(investment_id, as_of_date,
bucket)` with a NOT NULL `basis` discriminator. ADR-0079 also recorded an
explicit correction: the pre-existing composition-weight tables are **not**
time-series.

The three legacy tables are point-in-time:

- `investment_sector_weights` — natural key `(investment_id, sector_id)`,
  `weight_pct` (0–100), no `as_of_date`, no `basis` (b007, ADR-0045 §2).
- `investment_country_weights` — natural key `(investment_id,
  country_iso_code)`, same shape (b007). Currently unpopulated in normal
  operation; reserved for ISO-granular GP-report sources (roadmap A2/A3).
- `investment_region_weights` — natural key `(investment_id, region_id)`,
  same shape (b009, ADR-0046). Written by the Excel import path.

Point-in-time weights **overwrite themselves**: each manager report replaces
the composition and the prior generation is lost. That is acceptable only as
long as composition is refreshed at most once per source. It stops being
acceptable the moment the platform wants to (a) show composition drift over
time — the same thing the Total-Return / equity surface already does for
NAV — (b) reproduce a historical snapshot at a reporting cut-off, or (c) feed
Brinson-style attribution, which needs a per-period weight. ADR-0079 left
"whether the legacy sector/region/country weights should be historised" open
on YAGNI grounds. The accuracy need for the Total-Return surface has now
crossed that threshold, so the decision is taken here.

This ADR leaves the codebase with **one** weight convention again, aligned to
the ADR-0079 time-series pattern, rather than two.

### What the read path already tolerates

The pure analytics layer is indifferent to historisation. `aggregate_region_breakdown`
and `aggregate_sector_breakdown` (`services/analytics/portfolio_aggregation.py`)
consume a `dict[investment_id -> list[WeightDTO]]` and read only `weight_pct`
and the dimension id; they never reference `as_of_date`. As long as the
repository hands them the rows of a **single** snapshot per investment, the
breakdown maths and the analytics-purity invariant (ADR-0013/0045) are
unaffected. The "which snapshot is current" resolution therefore belongs in
the repository (DB-side, permitted), never in `services/analytics/` (which
must stay DB-free).

### What is missing at the write boundary

The Excel import block carries composition as a single **undated** block per
investment: `ImportedSectorWeight` / `ImportedRegionWeight` hold only the
dimension id and `weight_pct`. Importing genuinely multi-period composition
needs an Excel-format change, which is **deliberately deferred to a successor
chat** (the "second step"). This ADR establishes the historised schema and
anchors each imported composition to a single, honestly-derived `as_of_date`,
so the destructive-overwrite problem is fixed immediately while multi-period
import is unlocked later additively.

## Decision

Four coordinated changes across all three tables, applied as one ADR and one
Alembic migration so the pattern lands identically.

### 1. Historise the schema — `as_of_date` and `basis` on all three tables

Each table gains:

- `as_of_date DATE NOT NULL`, promoted into the natural key.
- `basis TEXT NOT NULL CHECK (basis IN ('reported','computed'))`, matching the
  ADR-0079 seam. For Excel-imported composition `basis = 'reported'`; the
  `computed` value is reserved for a future holdings-aggregation source, with
  no schema change when it arrives.

New natural keys (replacing the old point-in-time unique constraints):

| Table | Old unique constraint | New unique constraint | New key |
|---|---|---|---|
| `investment_sector_weights` | `uq_investment_sector_weights_investment_sector` | `uq_investment_sector_weights_investment_date_sector` | `(investment_id, as_of_date, sector_id)` |
| `investment_region_weights` | `uq_investment_region_weights_inv_region_unique` | `uq_investment_region_weights_investment_date_region` | `(investment_id, as_of_date, region_id)` |
| `investment_country_weights` | `uq_investment_country_weights_investment_country` | `uq_investment_country_weights_investment_date_country` | `(investment_id, as_of_date, country_iso_code)` |

RLS policies, audit triggers and the `ix_*_tenant_investment` /
`ix_*_tenant_<dim>` indices already exist on all three tables (b007/b009) and
are **not** re-applied; `ADD COLUMN` does not disturb them.

### 2. The `as_of_date` anchor — latest actual NAV, no silent fallback

Both at write time and at migration backfill, a composition snapshot is
anchored to the **latest `actual` NAV `as_of_date`** of the same investment.
Rationale: it is the honest "this is what last held" date, it is already
available in the same transform / in `investment_navs`, re-import is
idempotent (same date ⇒ upsert), and a genuinely new statement period (a later
latest-NAV) naturally lays down a *new* snapshot once the step-2 Excel format
distinguishes composition per date — drift capture for free.

The two boundaries handle a missing anchor differently, on purpose:

- **Migration backfill** must resolve every existing row (the column is NOT
  NULL). If any weight row belongs to an investment with no `actual` NAV, the
  migration **aborts loudly**, naming the offending `investment_id`s. A
  sentinel date is explicitly rejected (no-silent-fallback). This is a
  one-shot data-integrity gate.
- **Import write path** is resilient: an investment with no `actual` NAV to
  anchor on has its composition **skipped** with an explicit `ExtractionWarning`,
  rather than aborting the whole import. The composition is simply not written
  and the reason is surfaced.

### 3. One unified repository surface across the three repos

The legacy repos had drifted (`replace_for_investment` vs
`upsert_for_investment`; `list_by_investments` vs `list_for_investments`). They
are converged on a single contract, snapshot-aware and consistent with the
ADR-0079 readers:

- `list_for_investment(investment_id, *, as_of_cutoff=None) -> list[DTO]` —
  full history.
- `list_by_investments(investment_ids, *, as_of_cutoff=None) -> dict[UUID, list[DTO]]` —
  full history, batched. (Renamed from sector/country `list_for_investments`.)
- `list_latest_for_investment(investment_id, *, as_of_cutoff=None) -> list[DTO]` —
  the rows of the single most-recent snapshot (`max(as_of_date) <= cutoff`).
- `list_latest_by_investments(investment_ids, *, as_of_cutoff=None) -> dict[UUID, list[DTO]]` —
  the latest snapshot per investment, batched; every id present.
- `replace_snapshot_for_investment(investment_id, as_of_date, weights, *, basis, created_by) -> list[DTO]` —
  atomic, **date-scoped** replace: `DELETE WHERE investment_id = X AND
  as_of_date = D`, then insert the new generation for `(X, D)`. Other snapshots
  are untouched. (Replaces both `replace_for_investment` and
  `upsert_for_investment`.)
- `delete_for_investment(investment_id) -> int` — purge **all** snapshots
  (unchanged semantics).

The output DTOs gain `as_of_date` and `basis`; the `*WeightInput` payloads are
unchanged (one snapshot shares one date and one basis, passed as call
parameters, not per row). `list_latest_*` returns the identical
`dict[inv_id -> list[DTO]]` shape the breakdown consumers already expect, so
the swap is one line per call site and the analytics layer is untouched.

### 4. Consumer swap to the latest-snapshot reader

Every current read of "the" composition is the **latest** snapshot:

- `PortfolioReviewService._load_region_weights_for` / `_load_sector_weights_for`
  → `list_latest_by_investments` (covers the Overview and Portfolio-Review
  routes, which load through the service).
- `web/routes/investments.py` single-investment detail → `list_latest_for_investment`.

The full-history readers are retained for the forthcoming drift surface and
Brinson attribution, which need every snapshot per period.

### Scope boundaries (deliberate non-changes)

- **Excel multi-period composition** is the separate second step (extractor +
  sample workbook). Step 1 writes exactly one snapshot per import, anchored per
  §2.
- Cosmetic asymmetries are left as-is to keep the migration's blast radius
  tight: `investment_region_weights.weight_pct` stays `NUMERIC(8,4)` (vs
  `(7,4)` on the siblings) and the region table keeps its lack of an
  `updated_at` column (the block-replace write needs no in-place update). These
  are noted, not fixed, here.

## Consequences

**Positive**

- Composition history is preserved: a new statement period no longer destroys
  the prior generation. Reporting-date snapshots become reproducible and
  per-period attribution becomes feasible later, additively.
- The codebase returns to a **single** weight convention, aligned to ADR-0079;
  the `basis` seam makes a later `computed` holdings source additive.
- Analytics-layer purity (ADR-0013/0045) is preserved: the snapshot-selection
  logic lives in the repository; `aggregate_*_breakdown` and
  `test_analytics_layer_pure` are unchanged.
- The three repositories now expose one consistent, snapshot-aware API.

**Negative**

- A migration that alters three populated tables (drop/add unique constraint,
  add two NOT NULL columns, backfill) with several read/write consumers — the
  consumer swap, not the migration, is the effort driver, as foreseen.
- Until the step-2 Excel change, only one snapshot per investment can be
  imported; true multi-period composition is not yet ingestible.

**Neutral**

- The migration's hard-abort integrity gate will fail on any tenant whose
  composition rows reference investments without an `actual` NAV. In the
  current sample dataset all 20 investments have actual NAVs, so it passes; the
  gate exists to protect production data.
- The dual Alembic head condition (`b013_super_admin_audit` and
  `b016_add_liquid_archetypes`) is pre-existing and unchanged; this migration
  chains off `b016` and does not introduce a merge.

## Tests

1. **Schema / natural key:** the new three-column unique constraint exists and
   the old two-column one is gone; `as_of_date` and `basis` are NOT NULL; the
   `basis` CHECK rejects out-of-set values. All three tables still pass the
   RLS-context invariants (ADR-0078, `test_rls_schema_invariants`).
2. **Historisation behaviour:** two `replace_snapshot_for_investment` calls
   with different `as_of_date`s for one investment leave **two** snapshots;
   `list_latest_by_investments` returns only the later one; `list_by_investments`
   returns both.
3. **Date-scoped replace isolation:** replacing the snapshot for date `D2` does
   not touch the rows for date `D1`.
4. **Latest cut-off:** `list_latest_*` with `as_of_cutoff=D1` returns the `D1`
   snapshot even when a later `D2` exists.
5. **Analytics continuity:** `aggregate_sector_breakdown` /
   `aggregate_region_breakdown` over a `list_latest_by_investments` result
   reproduce the pre-historisation breakdown for a single-snapshot dataset
   (bit-for-bit), and `test_analytics_layer_pure` stays green.
6. **Write-path anchor:** the import anchors composition to the latest `actual`
   NAV date; an investment with no `actual` NAV is skipped with a warning and
   writes no composition rows.
7. **Migration gate:** the backfill raises with the offending `investment_id`s
   when a composition row has no `actual` NAV to anchor on.

## Related

- ADR-0079 (liquid-asset archetypes — established the time-series weight
  pattern, the `basis` discriminator, and the open question this ADR closes);
  ADR-0045 / b007 (original point-in-time sector & country weights);
  ADR-0046 / b009 (region weights); ADR-0013 (analytics-layer purity);
  ADR-0066 (cash-flow-adjusted returns — the surface motivating drift
  accuracy); ADR-0078 (RLS in tenant context); ADR-0014 (conventional commits).
- `handover-weight-historisation.md` — the originating handover note.
- Successor, to be written: Excel-format extension for multi-period composition
  ingestion (the "second step"); per-period (Brinson) attribution.
