# ADR-0056: Limit-Set Historisierung via `effective_from`

- **Status:** Accepted
- **Date:** 2026-05-19
- **Deciders:** PortfoliFLOW project owner
- **Tags:** schema, limits, historization, immutability, anlagegrenzen, phase-7

---

## Context

The investment-limit monitoring feature must handle the fact that
limit sets change over time and that historical evaluations must
remain reproducible. Two concrete examples motivate the
historisation requirement:

1. A Versorgungswerk amends its Satzung in 2024 to lift the equity
   cap from 25 % to 30 %. A portfolio review for Q1 2024 must
   evaluate coverage against the 25 % cap; a review for Q3 2024
   must evaluate against the 30 % cap. The same evaluation re-run
   in 2026 must produce the same Q1 2024 result as it did at the
   time.
2. The legislator amends the AnlV in mid-2025: a new Nr. 17
   sub-category is split out from Nr. 13. Investments previously
   counting against the Nr. 13 ceiling now belong to Nr. 17 going
   forward; the historical Nr. 13 evaluations for periods before
   the amendment must remain comparable across years.

Three structural options were considered:

**Option A — Mutable limit set with audit-only history.** One
``limit_sets`` row per family, edited in place. Historical
evaluations rely on the audit log to reconstruct what the values
were at any past date. The b001 audit trigger captures every
change.

**Option B — Effective-from versioning.** Each amendment creates
a new ``limit_set`` row with an ``effective_from`` date. Selecting
the applicable set at date ``t`` is ``MAX(effective_from) WHERE
effective_from <= t``. Old rows are immutable; the system never
deletes a limit set that was ever effective.

**Option C — Bitemporal (effective + transaction time).** Every
row carries both an ``effective_from`` and a ``recorded_at`` to
distinguish "the rule was changed" from "we learned the rule was
changed". Full audit trail, full retroactive correction support,
significant complexity.

A second decision dimension is the **per-class storage shape**:

- **B1 — Limits-as-rows.** One row per (limit set, class key,
  pct). A limit set of 8 asset classes is 8 rows in ``limits``.
- **B2 — Limits-as-JSON.** One row per limit set with a JSONB
  ``thresholds`` column mapping class key → percentage.

The third decision dimension is the **class-key resolution**:

- A SAA (Satzung / Strategic Asset Allocation) limit set has class
  keys that are asset-class codes ("equities", "private_equity", ...).
- An AnlV limit set has class keys that are AnlV codes
  ("anlv_13", "anlv_15", ...).

The two families share zero class-key space. The schema must
distinguish them — either with two physically separate tables
(one per family) or with a discriminator column on a single table.

---

## Decision

PortfoliFLOW adopts **option B (effective-from versioning) with
option B1 (limits-as-rows) and a family discriminator on a single
table pair**.

### Schema

Two tables in the same migration:

**``limit_sets``** — one row per (family, effective_from) tuple:

| Column | Type | Notes |
|---|---|---|
| ``id`` | UUID | PK |
| ``tenant_id`` | UUID NOT NULL | RLS-policed |
| ``family`` | TEXT NOT NULL | ``'saa'`` (Strategic Asset Allocation / Satzungsgrenzen) or ``'anlv'`` — open to additions |
| ``effective_from`` | DATE NOT NULL | The first calendar day this set is in force |
| ``label`` | TEXT NOT NULL | Operator-readable, e.g. ``"SAA 2024-Q3"`` or ``"AnlV-Novelle 2025"`` |
| ``notes`` | TEXT NULL | Free-text annotation; e.g. statute paragraph reference |
| ``created_by`` | UUID NOT NULL | FK to ``users.id`` |
| ``created_at`` | TIMESTAMPTZ NOT NULL | ``server_default NOW()`` |
| ``updated_at`` | TIMESTAMPTZ NOT NULL | ``server_default NOW()`` |

Constraints:

- ``CHECK (family IN ('saa', 'anlv'))`` — extended via
  migration when new families are added. The ``'saa'`` discriminator
  covers Satzungsgrenzen (Versorgungswerk statute caps) and is named
  for the broader Strategic Asset Allocation concept the same caps
  embody in non-statutory institutional contexts.
- ``UNIQUE (tenant_id, family, effective_from)`` — a tenant cannot
  have two sets of the same family taking effect on the same day.
- ``apply_tenant_rls('limit_sets')``.
- Audit trigger from b001.

**``limits``** — one row per (limit_set, class_key) tuple:

| Column | Type | Notes |
|---|---|---|
| ``id`` | UUID | PK |
| ``tenant_id`` | UUID NOT NULL | RLS-policed (denormalised from ``limit_sets`` for row-local RLS evaluation, per ADR-0035 §3) |
| ``limit_set_id`` | UUID NOT NULL | FK to ``limit_sets.id``, ``ON DELETE RESTRICT`` |
| ``class_key`` | TEXT NOT NULL | The class identifier within the family. For ``saa``: an asset-class code from the per-tenant ``asset_classes.code`` catalogue. For ``anlv``: an AnlV code from the global ``anlv_categories.code`` catalogue. |
| ``max_pct`` | NUMERIC(7, 4) NOT NULL | Maximum share as a percentage, e.g. ``30.0000``. Stored as percentage points, not as a [0, 1] fraction. |
| ``created_at`` | TIMESTAMPTZ NOT NULL | ``server_default NOW()`` |
| ``updated_at`` | TIMESTAMPTZ NOT NULL | ``server_default NOW()`` |

Constraints:

- ``CHECK (max_pct > 0 AND max_pct <= 100)`` — a limit of 0 is
  meaningless (use absence to mean "no investment allowed") and a
  limit > 100 is a data error.
- ``UNIQUE (limit_set_id, class_key)`` — one ceiling per class per
  set.
- ``apply_tenant_rls('limits')``.
- Audit trigger from b001.

The ``limits`` table does not FK into ``asset_classes`` or
``anlv_categories``. The reason is family-polymorphic resolution:
``class_key`` is a string that the engine resolves against the
appropriate catalogue depending on the parent set's ``family``.
A foreign-key constraint that switched targets by sibling-row
discriminator is not expressible in PostgreSQL without writing a
``CONSTRAINT TRIGGER``, which is more brittle than the integrity
check the engine performs in code on import.

### Validation rule: sum-to-100

On import, every limit set's percentages must sum to exactly 100 %
within a small tolerance (``±0.01``). This validation is enforced
in the importer, **not** in the database — the database accepts
any positive sum, because future extensions (V2: regulatory caps
that don't tile the space) need the flexibility.

The importer raises ``LimitValidationError`` if:

- A set's percentages sum outside ``[99.99, 100.01]``.
- A class key referenced in ``limits`` is unknown in its catalogue.
- Two rows in the same set carry the same ``class_key`` (caught
  earlier by the UNIQUE constraint, but the importer gives a
  better error message).

### Selection at evaluation time

The engine resolves the applicable limit set for a (family, date)
query as:

```sql
SELECT id
FROM limit_sets
WHERE tenant_id = :tenant_id
  AND family = :family
  AND effective_from <= :as_of_date
ORDER BY effective_from DESC
LIMIT 1
```

If no row qualifies (evaluation date precedes any imported limit
set), the engine raises ``LimitSetNotEffective`` and the operator
sees a surfaced error.

### Immutability after import

A limit set, once persisted, is **never modified**. The only
permitted operations:

- INSERT a new limit set with a later ``effective_from``.
- UPDATE the ``label`` or ``notes`` field — documentary only, not
  semantic. The audit trigger records the change.
- DELETE only via a future explicit "rollback recent import"
  workflow, which is out of scope for V1.

In particular, the ``limits`` rows belonging to an existing set
are never modified. A correction to a wrongly-imported set
requires a new set with a later ``effective_from`` that supersedes
it. This makes historical re-evaluation deterministic by
construction: the data the engine sees today is the data it saw
yesterday.

The Phase-V1 web UI does not include an editor for limit sets.
The only sanctioned ingestion path is Excel import, which appends
new sets and never modifies old ones. An "edit limit set" feature
is explicitly deferred to V2 and will require its own ADR
addressing how immutability is squared with operator-driven
correction.

---

## Rationale

**Why not option A (mutable + audit-only)?**

Three concrete failures:

1. Reproducing a Q1 2024 evaluation in 2026 requires reconstructing
   the limit-set values from the audit log. The audit-log row
   format is row-level diffs; reconstructing the **set** of rows
   that existed on a given day requires a transactional replay,
   which is not a normal query and not a normal join. Building
   evaluation logic on top of audit-log archaeology is brittle.
2. The audit log is intentionally a tail — old rows can in
   principle be pruned for storage. Limit-set history must persist
   for the regulatory retention period (typically 10 years for
   Versorgungswerke). Coupling regulatory retention to audit-log
   retention conflates two separate retention policies.
3. The b001 audit trigger captures changes; it does not capture
   "this row was created and never touched again". A limit set
   that was always 30 % from day one produces no audit rows. The
   absence of audit information for the unchanged case is correct
   trigger behaviour but useless for historical evaluation.

**Why not option C (bitemporal)?**

Bitemporal is the correct answer for systems that need to
distinguish "we believed X on date D1, then learned on D2 that
Y had been true since D0". That distinction matters for trade
booking and regulatory reporting where retroactive corrections
must remain visible. For limit-set monitoring, the distinction
adds complexity without operational payoff: the operator does not
benefit from preserving "the system thought the equity cap was
25 % until last Tuesday when we corrected it to 30 %". The
correction simply gets the right number into force.

The bitemporal path also amplifies the number of rows by the
correction count, and the query patterns for "what was the
effective set on date D" become a non-trivial window query rather
than a one-line SELECT.

**Why option B1 (limits-as-rows) over B2 (limits-as-JSON)?**

Three reasons:

1. Row-level audit. The b001 audit trigger captures one audit row
   per limit change. With JSONB, every change is a single audit
   row containing two JSONB blobs whose diff must be computed at
   read time. The row form gives free, granular history.
2. Engine query shape. The engine computes coverage per class:
   ``SELECT class_key, max_pct FROM limits WHERE limit_set_id = ?``.
   This is a 5–20 row scan that returns to the engine as a
   ``Dict[str, Decimal]``. With JSONB, the engine reads one row and
   parses the JSON. The row form is simpler and is the same shape
   the audit trail wants anyway.
3. UNIQUE constraint on ``(limit_set_id, class_key)``. JSONB has no
   such constraint at the database layer; the importer would have
   to enforce uniqueness itself. The row form gets the constraint
   for free.

**Why one ``limit_sets`` table for both families rather than two?**

The two families share identical structural concerns:
``effective_from`` versioning, ``label``/``notes`` metadata,
tenant-scoping, audit. A single ``limit_sets`` table with a
``family`` discriminator captures the commonality. Querying "the
applicable set for family X on date D" is the same query
regardless of family — the engine has one code path, not two
near-duplicates.

A future third family (e.g. internal investment-committee caps,
or regulatory caps from a non-AnlV jurisdiction) adds a row to
the ``CHECK`` constraint and a catalogue table for its class
codes. No new table, no new engine code path.

**Why ``effective_from`` and not ``effective_from`` + ``effective_to``?**

The "until" date is implicit: a set is in force from its
``effective_from`` until the next set of the same family supersedes
it. Storing both endpoints would require maintaining an invariant
that adjacent rows' dates align — an operational burden with no
analytical payoff. The engine's ``ORDER BY effective_from DESC
LIMIT 1`` query resolves the right set without any "to" column.

**Why percentage points (e.g. 30.0000) and not a [0, 1] fraction?**

Excel cells from a Satzung document read ``"30%"`` or ``"30,00 %"``
and convert to ``0.30`` when openpyxl interprets the percent
format. The importer normalises to percentage points (multiply by
100 if the value is in [0, 1]) so the database storage matches the
operator's mental model. The coverage engine works in pp end-to-end
to avoid double-conversion bugs. The decimal precision NUMERIC(7,
4) accommodates up to four decimal places (e.g. 33.3333 %), which
covers the realistic case of three-way equal splits in regulatory
ceilings.

### Edge cases worth pinning

- **Overlapping ``effective_from`` within a family.** Forbidden
  by the ``UNIQUE (tenant_id, family, effective_from)`` constraint.
  The operator cannot create two Satzung sets that both take
  effect 2024-07-01.
- **A new limit set imported with an ``effective_from`` in the
  past.** Permitted. The new set retroactively becomes the
  applicable set for evaluation dates from ``effective_from``
  onward (until the next set supersedes it). Historical
  evaluations re-run after such an import will produce different
  results — which is the intended semantics of "we forgot to
  enter the 2023 amendment, here it is now". The audit log
  records the INSERT and the operator's identity.
- **A limit set with no ``limits`` rows.** Permitted by the schema,
  rejected by the importer's sum-to-100 validation. The schema
  accepts it because a future "empty placeholder set" might have
  legitimate use cases (e.g. signalling a regulatory pause).
- **AnlV catalogue extension mid-history.** A new AnlV code added
  in 2025 cannot retroactively appear in a 2023 Satzung set. The
  engine evaluates older sets against the AnlV catalogue as it
  existed at evaluation time only via the ``class_key`` string —
  not via FK resolution. If the catalogue table loses a code
  (unsupported in V1), old sets referencing it would fail to
  resolve at evaluation time; the test suite includes a
  regression guard against catalogue shrinkage.

---

## Consequences

### Positive

- Historical evaluations are deterministic by construction. The
  engine queries the database it has today and produces the result
  it would have produced on the original evaluation date.
- The audit log captures changes for free, granular history without
  carrying the burden of being the source of truth for history.
- Adding a new limit family (e.g. internal IC caps in V2) costs
  one CHECK-constraint amendment and one new catalogue table.
- The 100 % validation lives in the importer, not the schema. V2
  use cases that need non-tiling caps (e.g. multiple overlapping
  regulatory ceilings on the same class) can be added without
  schema migration — only by relaxing the importer's validation
  per family.

### Negative

- No edit path for a recently-imported wrong set. The operator must
  import a corrective new set with the same ``effective_from``,
  which is blocked by the UNIQUE constraint and must be resolved
  by either (a) using a day-later ``effective_from`` if the wrong
  set is one day old or (b) the deferred V2 rollback workflow.
  The operational pain of this case is bounded by the cadence of
  limit-set changes — typically once a year — and is the price
  for the immutability guarantee.
- Operators cannot mass-edit class keys (e.g. rename
  ``"global_equity"`` to ``"world_equity"`` in the asset-class
  catalogue and have the historical limit sets follow). The
  ``class_key`` is a string snapshot, not a foreign key. This is
  the right tradeoff — renames should be additive, not
  retroactive — but it means catalogue stewardship is a real
  concern.

### Neutral

- The ``limits.tenant_id`` column denormalises ``limit_sets.tenant_id``
  for RLS row-local evaluation, per the established pattern from
  ``investment_navs``, ``investment_country_weights``, and
  ``investment_region_weights``. This pattern's cost (the
  denormalisation must be kept in sync) is paid by the
  ``LimitsRepository`` always setting both via INSERT, never via
  UPDATE.

---

## Implementation pointers

- New migration: ``db/migrations/versions/YYYY_MM_DD_HHMM_bNNN_add_limit_sets_and_limits.py``.
- New ORM models: ``core/models/limit_set.py`` and
  ``core/models/limit.py``.
- New repository: ``core/repositories/limits_repository.py``, with
  methods ``create_set_with_limits`` (transactional, single API
  surface for the importer), ``get_effective_set`` (the
  ``effective_from <= as_of_date`` lookup), ``list_sets``,
  ``list_limits``.
- New exception: ``LimitValidationError`` (importer) and
  ``LimitSetNotEffective`` (engine) — extend the existing
  ``PortfoliFLOWError`` hierarchy.
- Importer: extend ``data_import.py`` to recognise two new sheets,
  ``"Limit Set SAA"`` and ``"Limit Set 2"`` (sheet names pinned in
  the parser sub-stream of this work; ``"Limit Set 2"`` carries the
  ``'anlv'`` family in v21 testdata and a future ``"Limit Set 3"``
  would carry an internal-IC family).
- Tests:
  - Roundtrip: import → query effective set on three dates spanning
    two sets → assert the right set wins.
  - Validation: sum-to-99 / sum-to-101 / unknown class key / dup
    class key all raise ``LimitValidationError``.
  - Regression: importing the same workbook twice raises a
    ``UNIQUE`` violation (no silent overwrite of an immutable set).

---

## Related ADRs

- ADR-0035 — Multi-tenant operation with RLS (the RLS pattern this
  schema follows)
- ADR-0042 — Asset-class catalogue per tenant (the catalogue
  ``class_key`` resolves against for ``family = 'saa'``)
- ADR-0055 — Cash as residual in AUM coverage engine (the
  denominator the limits will be compared to)
- ADR-0057 — AnlV classification as 1:1 attribute (the catalogue
  ``class_key`` resolves against for ``family = 'anlv'``)
