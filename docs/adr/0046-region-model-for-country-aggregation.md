# ADR-0046: Region Model for Country Aggregation

- **Status:** Accepted
- **Date:** 2026-05-12
- **Deciders:** PortfoliFLOW project owner
- **Tags:** schema, regions, countries, excel-import, portfolio-review, phase-6, anti-debt

---

## Context

The Phase-5 closure-debug stream (sub-stream `closure-debug.*`, not
captured by a dedicated ADR) needed an end-to-end Excel import to
land in `investment_country_weights` so the Portfolio Review country
treemap could render against fresh tenant data. The V2 testdata
workbook (`PortfoliFLOW_Testdaten_v19.xlsx`) does not carry ISO
country codes in the country block — Excel ships **region buckets**
like `"DACH"`, `"Asia Emerging"`, and `"North America — USA"`. The
expedient was a Stufe-A auto-create path that wrote those region
labels into the `countries` stammtabelle as fake non-ISO rows and
referenced them from `investment_country_weights.country_iso_code`.

This was acknowledged as **technical debt** at the time of merge.
The `countries` stammtabelle is the project's anchor to the ISO
3166-1 alpha-2 standard (Bloomberg, MSCI, FactSet, GP-report
ingestion in roadmap A2/A3 all key on ISO). Polluting it with
free-text region tokens breaks that anchor and leaves
`investment_country_weights` in a hybrid state where the FK target
silently switches semantics row-by-row.

The Phase-6 closure work needs a clean separation:

1. The `countries` table remains the ISO-3166-1-alpha-2 stammtabelle
   only.
2. The Excel V2 import path writes against a **region aggregation
   layer** that is the proper schema home for region-level data.
3. `investment_country_weights` survives in the schema but is
   reserved for ISO-granular sources that will materialise in
   roadmap A2/A3 (GP-report scrapers reading `"60% United States, 40%
   Germany"` per investment).

A second concern: there are now two distinct concepts that both
naturally read as "region" in conversation. The `investments.region`
column is **strategy free-text** at the investment level (Excel row
4 `"Region"` → `"Europe / Global"`, `"Global / USA"`). The new
aggregation layer is **tenant-scoped, structured**, and disjointly
partitions ISO countries. Both must coexist without collision in
code, docs, and operator vocabulary.

---

## Decision

Introduce three new schema objects under migration **b009**
(`db/migrations/versions/2026_05_12_2139_b009_add_regions_table.py`):

- **`regions`** — per-tenant catalogue of region definitions.
  Each row is `(id, tenant_id, code, display_name, description,
  sort_order, ...)`. Pre-seeded by `portfoliflow bootstrap` from a
  hard-coded list in `cli/bootstrap.py::_DEFAULT_REGIONS`. The Excel
  import path **never** auto-creates regions; unknown labels are
  hard import errors.

- **`region_country_memberships`** — many-to-one mapping between
  regions and ISO countries.
  `UNIQUE (tenant_id, country_iso_code)` enforces the **M1
  strict-partition invariant**: every ISO country belongs to at
  most one region per tenant. Switching to many-to-many (M2) later
  would only require dropping that constraint; the data model is
  forward-compatible.

- **`investment_region_weights`** — per-investment region
  allocation. Mirrors `investment_country_weights` in shape; the
  Excel V2 import path writes here.

The legacy `investment_country_weights` table remains in the
schema with a `COMMENT ON TABLE` that documents its reduced scope.
The Excel-import path no longer writes it; future ISO-granular
sources (roadmap A2/A3) will.

The Phase-5a Stufe-A debris is removed in the same migration in
two cleanup steps that run before the new tables are created:

1. Delete every `investment_country_weights` row whose
   `country_iso_code` is not a real two-letter uppercase ISO code.
2. Delete every `countries` row whose `iso_code` is not a real
   two-letter uppercase ISO code (the auto-created `"dach"`,
   `"north_america_usa"`, … tokens).

The cleanup steps are **not reversible** — `downgrade` cannot
reconstruct the deleted debris rows. That is acceptable: the
deleted rows were debt, not data.

### Default region catalogue (M1)

Twelve regions, pre-seeded for every tenant. Codes are stable;
display names are operator-visible:

| `code` | `display_name` | Member ISO codes |
|---|---|---|
| `dach` | DACH | DE, AT, CH, LI |
| `uk_ireland` | UK & Ireland | GB, IE |
| `nordics` | Nordics | DK, SE, NO, FI, IS |
| `western_europe_other` | Western Europe ex-DACH/UK/Nordics | FR, IT, ES, BE, NL, LU, PT, MC, MT, CY, GR |
| `cee` | Central & Eastern Europe | PL, CZ, HU, SK, RO, BG, HR, SI, BA, RS, ME, MK, AL, EE, LV, LT, UA, BY, MD, XK |
| `north_america_usa` | North America — USA | US |
| `north_america_canada` | North America — Canada | CA |
| `latin_america` | Latin America | BR, MX, AR, CL, CO, PE, UY, EC, BO, PY, VE, CR, PA, DO, GT, HN, NI, SV, CU, JM, TT, HT |
| `apac_developed` | Asia-Pacific Developed | JP, KR, AU, NZ, SG |
| `greater_china` | Greater China | CN, HK, TW, MO |
| `asia_emerging` | Asia Emerging | IN, ID, TH, MY, PH, VN, PK, BD, LK, KH, LA, MM, MN, NP |
| `mea` | Middle East & Africa | SA, AE, IL, EG, QA, KW, OM, BH, JO, LB, TR, ZA, NG, KE, MA, TN, DZ, ET, GH, CI, SN, TZ, UG, AO, ZM, ZW |

**Disputed-bucket decisions:**

- Hong Kong (HK), Macau (MO), Taiwan (TW) → `greater_china`, not
  `apac_developed`. The Excel "Asia-Pacific Developed" bucket is
  assumed to exclude the China-adjacent markets.
- Turkey (TR) → `mea`, not `cee`. MSCI convention.
- Australia (AU), New Zealand (NZ) → `apac_developed`.
- Russia (RU) and Kazakhstan (KZ) are deliberately **omitted**.
  Market access for RU is sanctioned; KZ is ambiguous. Should a
  later Excel input require one of them, a bootstrap update with
  a revision-history entry is required.

### Two concepts named "region" — disambiguation

| Construct | Where | Meaning |
|---|---|---|
| `investments.region` | Investment row, free-text | Strategy geography ("Europe / Global"). Not aggregated. Not part of the partition. |
| `regions` table (this ADR) | Per-tenant aggregation layer | Disjoint groups of ISO countries. Excel region labels resolve here. |

Operators and code must avoid the bare term "region" without
qualification when both could apply. The CLAUDE.md glossary
entries for `investments.region` (existing) and the new
`regions` table (added in the same PR) carry this distinction.

---

## Rationale

**Why a dedicated aggregation layer rather than another
`country.region_default` column?**
The existing `countries.region_default` field is descriptive
free-text ("DACH", "Asia Pacific Developed") attached per ISO
country. It is not normalised, not tenant-scoped, and not a
foreign-key target — so it cannot anchor `investment_region_weights`
rows. Promoting it in place would still leave the per-tenant
override case (a tenant that wants to combine UK and Ireland into
"British Isles" rather than separating them) unsolved.

**Why strict partition (M1) and not many-to-many (M2)?**
The Excel block in `PortfoliFLOW_Testdaten_v19.xlsx` reports each
region as a percentage of one investment's exposure; the rows
visibly sum to ≤ 100%. Treating regions as overlapping groups
would force the aggregation engine to disambiguate, while no
input today actually demands that flexibility. M1 keeps the
aggregation arithmetic identical to the sector path and trivial to
audit. Migration to M2 is a one-line constraint drop.

**Why hard-fail on unknown Excel labels rather than soft-fallback?**
The asset-class and sector paths use Stufe-A auto-create on the
grounds that Excel **is** the canonical source for those
vocabularies. Regions are different: the operator does not
introduce new regions ad hoc during routine import; the catalogue
is meant to be stable and team-curated. A typo (`"Northern
America"` for `"North America — USA"`) should fail the import so
the operator fixes the cell, not silently land in a never-rendered
bucket.

**Why keep `investment_country_weights` at all?**
The roadmap A2/A3 work (GP-report scrapers) will deliver
ISO-level allocations directly. Wiping the table now would force
re-adding it later. The `COMMENT ON TABLE` documents the reduced
scope so a future contributor reading the schema sees the gate.

---

## Alternatives Considered

- **A. Keep the Stufe-A auto-create path indefinitely.** Rejected:
  pollutes the ISO stammtabelle, breaks the FactSet/MSCI anchor,
  and forces every downstream consumer to filter "real" codes
  from auto-created tokens.
- **B. Replace `countries` entirely with the new region table.**
  Rejected: throws away the ISO anchor that the GP-report
  scraper, FactSet feed, and any future MSCI integration depend
  on.
- **C. Store region weights in a JSONB blob on `investments`.**
  Rejected: opaque to SQL aggregation, no per-row audit, and
  inconsistent with the relational shape of sector and country
  weights.
- **D. M2 many-to-many on day one.** Rejected as premature
  complexity. M1 is the smallest model that the data shape
  supports. Promotion to M2 is one constraint drop.

---

## Consequences

**Operational:**

- After this sub-stream lands, the operator must run a DB reset
  (`./scripts/db-reset.sh`), Alembic upgrade, `portfoliflow
  bootstrap`, and a fresh Excel re-import. The cleanup steps in
  the migration delete the closure-debug debris on first run.
- New regions cannot be introduced by Excel edits. A new market
  bucket requires editing
  `cli/bootstrap.py::_DEFAULT_REGIONS` and recording the change
  in this ADR's revision history.

**Code-shape:**

- `aggregate_country_breakdown` → `aggregate_region_breakdown`.
- `CountryBreakdown` / `CountryBreakdownRow` →
  `RegionBreakdown` / `RegionBreakdownRow`.
- `services/chart_specs/portfolio_review_country_treemap.py` →
  `portfolio_review_region_treemap.py`, exporting
  `build_region_treemap_spec`.
- `PortfolioOverviewBundle.country_breakdown` → `region_breakdown`.
  Same for `SingleInvestmentReviewBundle`.
- `InvestmentExtractor.extract_country_weights` →
  `extract_region_weights` with a `regions_by_display_name: dict[str,
  UUID]` lookup. Unknown labels raise `ImportRowError`.
- `InvestmentService.transform_upload_to_investments` accepts
  `region_repository` and `region_weights_repository` in place of
  the country counterparts.
- The Portfolio Review section tile changes ID from `pr-tile-4` to
  `pr-tile-region` and title from "Country split" to "Region
  split".
- The investment-detail surface renders a Region Allocation table
  instead of the prior Country Allocation table.

**Compliance & Audit:**

- The new tables carry the standard audit trigger and
  `apply_tenant_rls` policy. Region writes are auditable.
- `investment_region_weights` rows reference `regions.id` with
  `ON DELETE RESTRICT`; deactivating a region requires explicit
  remediation rather than silent cascade.

---

## References

- ADR-0009 (V2 Excel format) — defines the row blocks the import
  path parses.
- ADR-0043 (Investment domain & Excel transformation) — defines
  the asset-class auto-create pattern this ADR diverges from for
  regions.
- ADR-0045 (Charts/Statistics web migration & analytics service
  foundation) — superseded for the country-weights pathway by
  this ADR. The Phase-5a soft-fallback semantics for the
  Excel-import country path no longer apply.
- Roadmap item A2/A3 (GP-report scrapers) — primary future writer
  of `investment_country_weights`.
- CLAUDE.md glossary — region (free-text on investment) vs
  `regions` table.

---

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-05-12 | Project owner + assistant | Initial decision. M1 strict-partition. Twelve default regions seeded by bootstrap. |
