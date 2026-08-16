# ADR-0057: AnlV Classification as 1:1 Investment Attribute

- **Status:** Accepted
- **Date:** 2026-05-19
- **Deciders:** PortfoliFLOW project owner
- **Tags:** schema, anlv, investments, classification, regulatory, anlagegrenzen, phase-7

---

## Context

The German Anlageverordnung (AnlV) is the supervisory regulation
that governs how Versorgungswerke (and other regulated
institutional investors covered by the corresponding statutory
framework) may allocate their guaranteed-asset cover funds across
asset categories. Each numbered paragraph of § 2 Abs. 1 AnlV
defines an admissible asset category with its own maximum share
of the total cover funds:

- Nr. 13 — Unternehmensbeteiligungen (unlisted private equity)
- Nr. 14 — Immobilien (real estate, direct and indirect)
- Nr. 15 — Aktien (listed equities)
- Nr. 16 — Investmentvermögen (open-ended investment funds)
- Nr. 17 — Sonstige Beteiligungsanlagen (other equity-style
  participations; PE fund-of-funds typically map here)
- ... and roughly twenty more categories covering bonds, loans,
  mortgages, deposits, currency hedges, etc.

The investment-limit monitoring feature evaluates each
investment's contribution against the ceiling of its AnlV
category. The data-modelling question is: **how is the assignment
of an investment to its AnlV category represented?**

Three modelling options were considered.

**Option A — 1:1 attribute on ``investments``.** Add a new column
``anlv_code_id`` to the ``investments`` table that points at a new
catalogue table ``anlv_categories``. Each investment has exactly
one AnlV code (or NULL if unclassified).

**Option B — Weighted assignment via bridge table.** A new
``investment_anlv_weights`` table allowing one investment to
split its NAV across multiple AnlV codes with explicit weights,
analogous to ``investment_sector_weights`` and
``investment_region_weights``.

**Option C — Effective-from bridge.** A bridge table with
``investment_id``, ``anlv_code_id``, ``effective_from``. The
investment's category may change over time; coverage evaluation
joins on the temporal predicate ``effective_from <= as_of_date``.

A second decision dimension is the **catalogue scope**: global or
per-tenant? The AnlV is a federal regulation — its numbered
categories are identical for every Versorgungswerk in Germany.
This points strongly to a **global** stammtabelle (like
``countries``) rather than a per-tenant catalogue (like
``asset_classes``, ``sectors``, ``regions``).

A third decision dimension is **how new categories arrive** when
the AnlV is amended. The legislative cadence is rare (last major
amendment 2015, technical revisions roughly every 2–4 years), so
the operational mechanism can be lightweight — a JSON fixture seed
loaded by a migration, analogous to the ``iso_3166_1_alpha_2``
country fixture.

---

## Decision

PortfoliFLOW adopts **option A (1:1 attribute on investments)
with a global ``anlv_categories`` catalogue seeded from a JSON
fixture**.

### Schema

**New global catalogue table ``anlv_categories``:**

| Column | Type | Notes |
|---|---|---|
| ``code`` | TEXT | PK, e.g. ``"anlv_13"`` (snake_case, ADR-0008-compliant) |
| ``paragraph_label`` | TEXT NOT NULL | The legislative reference, e.g. ``"§ 2 Abs. 1 Nr. 13 AnlV"`` |
| ``display_name`` | TEXT NOT NULL | Operator-readable, e.g. ``"Unternehmensbeteiligungen"`` |
| ``description`` | TEXT NULL | Optional explanatory text |
| ``sort_order`` | INTEGER NOT NULL | For consistent listing order in UI |
| ``created_at`` | TIMESTAMPTZ NOT NULL | ``server_default NOW()`` |
| ``updated_at`` | TIMESTAMPTZ NOT NULL | ``server_default NOW()`` |

This table is **not RLS-protected** — every tenant reads the same
catalogue. The regression guard
``test_rls_schema_invariants`` carries an allow-list entry
documenting the exception, analogous to the ``countries`` table
(see ADR-0046 §Context and the b007 migration commentary).

The seed fixture lives at
``services/data_normalization/fixtures/anlv_categories.json``,
following the same pattern as ``iso_3166_1_alpha_2.json``. The
fixture covers every AnlV category numbered in § 2 Abs. 1, not
just the ones present in the V1 test data. Loading the full set
upfront avoids partial-catalogue failures when an operator imports
a workbook that references a previously-unused category.

**New column on ``investments``:**

| Column | Type | Notes |
|---|---|---|
| ``anlv_code`` | TEXT NULL | FK to ``anlv_categories.code``, ``ON DELETE RESTRICT`` |

The column is nullable. An investment without an AnlV
classification is the V1 "nicht zuordenbar" engine-fallback case
(see Kickoff #1 §Verbindliche Architektur-Entscheidungen item 5):
unclassified investments contribute to a synthetic
"AnlV unallocated" bucket at evaluation time but are not stored
as such — the bucket is reine Engine-Logik. NULL is the
representation in storage.

An index on ``investments(anlv_code)`` accelerates the engine's
``GROUP BY anlv_code`` aggregation.

### Excel import

The ``Attributes`` sheet gains a new row between "Asset Class" and
"Manager / Fund Name":

```
Row 7: Asset Class       | Equities  | Equities  | ...
Row 8: AnlV              | Nr. 15    | Nr. 15    | ...   ← NEW
Row 9: Manager / Fund... | ...
```

The importer maps the Excel cell value (``"Nr. 13"``) to the
catalogue code (``"anlv_13"``) via a small normalisation function
in the importer module. The normaliser accepts the
operator-friendly forms ``"Nr. 13"``, ``"Nr.13"``, ``"13"``, and
``"anlv_13"`` to be lenient against minor formatting variation;
all map to the canonical ``"anlv_13"`` code. An unrecognised
value raises ``DataImportError`` with the offending cell
identified.

Empty cells in the AnlV row are permitted and map to a NULL
``anlv_code`` on the investment row. Operators ingesting a
workbook for a portfolio that includes one or two unclassified
"other" investments do not have to fabricate an AnlV code.

### Currency note

Identical to the ADR-0055 treatment: NAVs land in EUR for actual
sheets; the AnlV coverage engine works on the EUR-NAV figures
already present in ``investment_navs``. No additional currency
arithmetic for AnlV.

---

## Rationale

**Why not option B (weighted assignment)?**

Two arguments — one regulatory, one operational.

*Regulatory.* The AnlV § 2 Abs. 1 categories are mutually
exclusive by legislative construction: a single investment vehicle
fits in exactly one category, determined by its legal form and
underlying assets. A real-estate fund is Nr. 14, a private-equity
limited partnership is Nr. 13, an UCITS open-ended fund is
Nr. 16. There is no admissible reading of the regulation in which
one investment counts partially against multiple ceilings. (The
look-through provisions for funds-of-funds are addressed through
separate reporting, not through AnlV-code splits.)

A weighted split would let the operator allocate "70 % to Nr. 13,
30 % to Nr. 16" for the same fund — which has no regulatory
meaning. Building a flexible model for a fact that is not flexible
in reality invites operator error.

*Operational.* The ``investment_sector_weights`` and
``investment_region_weights`` tables exist because sectors and
geographies *are* genuinely fractional at the investment level —
a PE fund holds many companies, distributed across sectors and
countries. The weighted model captures real-world granularity.
For AnlV, the classification is at the fund level, not the
holdings level. A bridge table would model a fractionality that
isn't there.

**Why not option C (effective-from bridge)?**

The case for option C is the rare event where a fund is
reclassified — e.g. a structural reorganisation moves an existing
participation from Nr. 13 to Nr. 17 in mid-life. Two factors make
the bridge table the wrong response:

1. The base ``investments`` table already carries audit history
   via the b001 trigger. A column-level UPDATE to ``anlv_code``
   produces one audit-log row capturing the change with timestamp
   and actor. Historical reconstructions for AnlV are answered by
   the audit log, the same way they are for ``manager_name``,
   ``region``, and other investment-level attributes that may
   change over an investment's life.
2. The coverage engine evaluates "what is the AnlV code on date
   D?" extremely rarely — the realistic answer is "today's value
   on every evaluation date". A bridge table optimised for
   per-date resolution would add joining cost to every coverage
   query without operational payoff.

If a future workflow needs to re-evaluate AnlV coverage as of an
arbitrary historical date with the AnlV codes as they were on
that date, the audit-log path is the right answer:

```sql
SELECT investment_id, max(new_value) FILTER (WHERE ...)
FROM audit_log
WHERE column_name = 'anlv_code'
  AND changed_at <= :as_of_date
GROUP BY investment_id
```

This is not a normal query, but it is a possible query, and the
schema does not need to optimise for the abnormal case.

**Why global rather than per-tenant catalogue?**

The AnlV is federal regulation. Every German Versorgungswerk
reads the same § 2 Abs. 1 Nr. 13. There is no legitimate per-
tenant variation. A per-tenant catalogue would invite divergence
("our Nr. 13 includes co-investments, but their Nr. 13 doesn't"),
which contradicts the regulation's purpose as a uniform
benchmark.

The ``asset_classes`` catalogue is per-tenant precisely because
asset-class taxonomies are operational choices that vary across
institutions. AnlV is the opposite: a uniform regulatory
nomenclature.

The trade-off cost — that PortfoliFLOW cannot serve non-German
regulated investors with the same column — is acceptable. A
future addressing this would introduce a parallel global
catalogue for the foreign jurisdiction (e.g. ``solvency_ii_codes``
for European insurance undertakings) and a corresponding
``limit_sets.family`` value. The schema's family-discriminator
design (ADR-0056) accommodates this without touching AnlV.

**Why ``code`` as the primary key rather than a UUID surrogate?**

Two arguments:

1. The code is genuinely the identifier in the legislation. Using
   UUIDs would introduce a layer of indirection between the
   regulatory reference and the data, harming readability of joined
   query results.
2. The ``countries`` table uses ``iso_code`` as PK for the same
   reason: ISO codes are the identity, not surrogates. Following
   the same convention for AnlV codes keeps the global-catalogue
   pattern internally consistent.

A migration that needs to rename a code (extremely unlikely — the
AnlV doesn't renumber its paragraphs) would have to cascade the
FK references; this is an acceptable rare-event cost.

**Why ``anlv_code`` directly on ``investments`` rather than via
the ``type_specific_data`` JSONB column?**

ADR-0043 §2 deliberately keeps ``type_specific_data`` unused in
Phase 4, reserved as an "emergency exit" for type-specific
extensions. AnlV classification is **not** type-specific (every
investment type can carry an AnlV code) and is a primary
query/aggregation dimension for a major feature. JSONB storage
would defeat the GROUP-BY performance and obscure a first-class
attribute in an opaque blob. The column belongs on the table.

**Why allow NULL rather than enforce classification?**

Two reasons:

1. The V1 Excel input may not always carry a complete AnlV
   classification. Forcing classification at import time would
   block import of otherwise-valid workbooks.
2. The "AnlV unallocated" engine-fallback bucket needs a
   representation. NULL is the natural one. Adding a sentinel
   row ``anlv_unallocated`` to the catalogue would muddy the
   semantics (the unallocated case is "no classification", not
   "a special classification") and would require the catalogue to
   distinguish real AnlV codes from sentinel codes — a
   complication for no benefit.

The importer logs the count of unclassified investments in the
operator-visible summary so an incomplete classification is
visible rather than silent.

---

## Consequences

### Positive

- The schema mirrors the regulatory reality: AnlV codes are
  uniform, mutually exclusive, and apply at the investment level.
- Aggregation queries (``SELECT anlv_code, SUM(nav_eur) FROM ...
  GROUP BY anlv_code``) are trivially indexed.
- Historical reclassifications are captured by the existing audit
  trigger without additional schema or query infrastructure.
- The global catalogue pattern matches ``countries`` and avoids
  per-tenant catalogue maintenance.

### Negative

- An investment that genuinely belongs to multiple AnlV categories
  cannot be represented. The author believes this case does not
  exist in practice; if a counter-example emerges, this ADR is
  superseded by a follow-up that introduces a weighted bridge
  table for the affected family while keeping the 1:1 column for
  the common case.
- A retroactive reclassification rewrites the ``anlv_code`` value
  in place. Historical evaluations re-run after such an UPDATE
  produce results consistent with the new classification, not the
  old one. The audit trail captures the change but the query path
  does not pick it up by default. This is the right behaviour for
  the realistic case ("the classification was always wrong, the
  evaluation should reflect the correct one") and the wrong
  behaviour for the hypothetical case ("the classification
  legitimately changed mid-life and historical reports should
  reflect the old code"). The latter case is rare enough to
  warrant the audit-log workaround rather than a schema cost.

### Neutral

- One row per AnlV category in the global ``anlv_categories``
  table — roughly 25 rows, never tenant-replicated. Storage
  footprint is negligible.
- The b001 audit trigger captures changes to ``investments.anlv_code``
  alongside changes to every other column, with no special
  configuration.

---

## Implementation pointers

- New migration:
  ``db/migrations/versions/YYYY_MM_DD_HHMM_bNNN_add_anlv_categories.py``.
  Creates ``anlv_categories`` (global, not RLS-protected), seeds
  from the JSON fixture using
  ``INSERT ... ON CONFLICT DO NOTHING`` for idempotency, then
  adds the ``anlv_code`` column to ``investments`` with FK and
  index.
- New JSON fixture:
  ``services/data_normalization/fixtures/anlv_categories.json``.
  Lists all numbered categories of § 2 Abs. 1 AnlV (not just the
  three used by the V1 test data) so the catalogue is complete
  on first migration.
- New ORM model: ``core/models/anlv_category.py``.
- New repository:
  ``core/repositories/anlv_category_repository.py``, read-only
  methods (``list_all``, ``get_by_code``). No write methods
  exposed to the application; updates come exclusively through
  migrations.
- Updated ORM model: ``core/models/investment.py`` gains the
  ``anlv_code`` column.
- Updated repository: ``core/repositories/investment_repository.py``
  gains ``anlv_code`` in its create/update DTOs and in its query
  projections.
- Updated regression guard: ``test_rls_schema_invariants``
  allow-list adds ``anlv_categories``.
- Importer: ``data_import.py`` parser for the AnlV row in
  ``Attributes`` (the row between "Asset Class" and
  "Manager / Fund Name"), with the normalisation function that
  maps ``"Nr. 13"`` → ``"anlv_13"``.
- Tests:
  - Roundtrip: import workbook with the new AnlV row → query
    ``investments.anlv_code`` → expected values.
  - Normalisation: ``"Nr. 13"``, ``"Nr.13"``, ``"  13 "``,
    ``"anlv_13"`` all map to ``"anlv_13"``.
  - Unknown code: cell value ``"Nr. 99"`` raises
    ``DataImportError``.
  - NULL path: empty AnlV cell results in NULL
    ``investment.anlv_code``.
  - RLS regression: ``anlv_categories`` is on the allow-list.

---

## Related ADRs

- ADR-0008 — English as the sole codebase language (the snake_case
  ``anlv_13`` codes; the German ``"Nr. 13"`` is operator-facing
  Excel input only)
- ADR-0035 — Multi-tenant operation with RLS (the allow-list
  pattern for non-RLS global tables)
- ADR-0042 — Asset-class catalogue per tenant (the contrasting
  pattern: per-tenant for operational vocabulary, global for
  regulatory nomenclature)
- ADR-0043 — Flat-polymorphic investments table (the table this
  ADR extends; the ``type_specific_data`` JSONB column is
  explicitly **not** the right home for AnlV)
- ADR-0046 — Region model for country aggregation (the global-
  catalogue + JSON-fixture + allow-list pattern is the same)
- ADR-0055 — Cash as residual in AUM coverage engine (the
  denominator the AnlV ceilings are compared against)
- ADR-0056 — Limit-Set Historisierung (the ``family = 'anlv'``
  limit sets whose ``class_key`` resolves to this catalogue)
