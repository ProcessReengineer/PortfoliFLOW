# ADR-0083: Correct the AnlV Category Catalogue to the § 2 Abs. 1 AnlV Statute

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** PortfoliFLOW project owner
- **Tags:** schema, anlv, regulatory, correction, data-fix

---

## Context

The global `anlv_categories` catalogue was introduced by ADR-0057 as a
1:1 classification attribute on `investments`, seeded from
`services/data_normalization/fixtures/anlv_categories.json` by migration
`b010` (`INSERT … ON CONFLICT DO NOTHING`). The catalogue is the
authoritative mapping from `investments.anlv_code` to the numbered asset
categories of § 2 Abs. 1 AnlV, and it is the key the live
investment-limit feature (#019, ADR-0055/0056/0057) groups by when it
evaluates the AnlV "Mischung" ceilings.

The seed's `display_name` and `description` values do **not** match the
actual § 2 Abs. 1 AnlV numbering. The numbering was re-verified against
the consolidated statute (Anlageverordnung of 18.04.2016, last amended
04.02.2026). Roughly thirteen of the eighteen numbered entries carry a
label for the wrong paragraph number. The most consequential
mismatches:

| Code | Seeded (wrong) label | Statute § 2 Abs. 1 Nr. N |
|---|---|---|
| `anlv_7` | "Asset-Backed-Securities und Credit-Linked-Notes" | Nr. 7 is *börsennotierte Schuldverschreibungen*; ABS/CLN are Nr. 10 |
| `anlv_12` | "Hochverzinsliche Schuldverschreibungen (High Yield)" | Nr. 12 is *notierte Aktien* |
| `anlv_13` | "Unternehmensbeteiligungen" | Nr. 13 is *Beteiligungen* (nicht notierte Anteile **und** geschlossene PE-AIF) |
| `anlv_15` | "Aktien" | Nr. 15 is *OGAW* (offene Publikumsinvestmentvermögen) |
| `anlv_16` | "Investmentvermögen (OGAW und AIF)" | Nr. 16 is *offene Spezial-AIF mit festen Anlagebedingungen* |
| `anlv_17` | "Sonstige Beteiligungsanlagen" | Nr. 17 is *andere Investmentvermögen* |
| `anlv_19` | "Edelmetalle" | **No Nr. 19 exists.** Precious metals are admissible only via the Öffnungsklausel (Abs. 2). The entry is fabricated. |

ADR-0057's own Context section reproduces the same mislabelling (it
states Nr. 13 = Unternehmensbeteiligungen, Nr. 15 = Aktien, Nr. 16 =
Investmentvermögen, Nr. 17 = Sonstige Beteiligungsanlagen). ADR-0057 is
an **Accepted** record and is immutable: it is the corrected
predecessor and stays unchanged for historical traceability. This ADR
is its successor.

The defect is not cosmetic. The #019 Mischung evaluation keys off this
catalogue, and the upcoming Regulatory Reporting Pre-Fill feature (#032,
Nw 670 / Anlage Mischung) builds its category rows directly on it. A
wrong § 2 Abs. 1 number is a regulatory-correctness defect, not a
display-string nicety.

Because the b010 seed is `ON CONFLICT DO NOTHING`, a corrected fixture
alone fixes only **fresh** databases (db-reset / `create-tenant` paths);
already-seeded databases keep the wrong labels until an explicit
relabel runs. Both are therefore required.

This decision is audit-relevant: it concerns the reproducibility and
correctness of a regulatory classification used in supervisory-limit
evaluation (BAIT/VAIT change-management traceability).

## Decision

PortfoliFLOW corrects the `anlv_categories` catalogue to the § 2 Abs. 1
AnlV statute as a **relabel-only** change:

1. **Codes are preserved.** `anlv_1` … `anlv_18` keep their primary-key
   values. Only `display_name`, `description`, and (where applicable)
   `paragraph_label` change. No `code` is renamed, so the
   `investments.anlv_code` foreign keys and the `limits.class_key`
   snapshots (ADR-0056) remain intact.
2. **The fabricated `anlv_19` ("Edelmetalle") is dropped**, guarded:
   the removal aborts loudly if anything references it (see below).
3. **Two regulatory buckets the BerVersV forms require are added:**
   `anlv_oeffnungsklausel` (§ 2 Abs. 2 AnlV) and `anlv_genehmigung`
   (§ 2 Abs. 3 AnlV). These carry non-numeric codes deliberately —
   they are not Abs. 1 numbered categories.
4. **The corrected JSON fixture** (`anlv_categories.json`) carries the
   statute-accurate values for fresh seeds.
5. **An explicit relabel migration** (`b018_fix_anlv_category_labels`)
   updates already-seeded databases. The migration is **self-contained**:
   it drives the UPDATE/INSERT/DELETE from literal lists defined in the
   migration module, **not** by re-reading the JSON fixture, so it stays
   reproducible regardless of later fixture edits. The `anlv_19` delete
   is guarded — it counts references in `investments.anlv_code` and in
   `limits` (family `'anlv'`, `class_key = 'anlv_19'`) first and raises
   `RuntimeError` with the counts and a remediation hint if either is
   non-zero, rather than skipping or force-deleting.

## Rationale

- **Regulatory correctness.** The whole point of the AnlV catalogue is
  to be a uniform, statute-faithful nomenclature (ADR-0057 §"Why
  global"). A catalogue whose labels point at the wrong paragraph
  numbers defeats that purpose and silently mis-evaluates the #019
  Mischung ceilings.
- **Relabel, not re-key.** Renaming a `code` would cascade into every
  `investments.anlv_code` FK and every immutable `limits.class_key`
  snapshot, breaking ADR-0056 historisation. The labels — not the codes
  — are wrong, so relabelling is both sufficient and the
  lowest-blast-radius fix.
- **Fixture *and* migration.** The `ON CONFLICT DO NOTHING` seed means a
  corrected fixture never overwrites existing rows. New deployments need
  the corrected fixture; existing ones need the explicit UPDATE. Neither
  alone is complete.
- **Self-contained migration.** Re-reading the fixture from a migration
  couples a historical schema step to a mutable file. Capturing both the
  corrected values and the prior values as literals keeps the migration
  reproducible and gives an exact, symmetric `downgrade()`.
- **Guarded delete.** Silently skipping or force-deleting a referenced
  `anlv_19` would violate the no-silent-fallback rule (ADR-0005). The
  loud failure with counts and remediation is the correct posture for a
  data-integrity step.
- **Add the Abs. 2 / Abs. 3 buckets now.** The BerVersV returns (#032)
  report the Öffnungsklausel and genehmigte Anlagen as their own lines.
  Seeding them with this correction avoids a second catalogue migration
  when #032 lands.

## Alternatives Considered

- **Corrected fixture only (no migration).** Rejected: the b010 seed is
  `ON CONFLICT DO NOTHING`, so existing databases — including the live
  Primary-Tenant deployment — would retain the wrong labels indefinitely.
- **Re-key the codes to the correct numbers (e.g. move "Aktien" from
  `anlv_15` to `anlv_12`).** Rejected: it would break
  `investments.anlv_code` FKs and the immutable `limits.class_key`
  snapshots (ADR-0056), and it conflates two separate problems — the
  catalogue labels (this ADR) and the *test-data tagging* (a separate
  follow-up, see Consequences).
- **Edit ADR-0057 in place.** Rejected: Accepted ADRs are immutable;
  corrections are recorded in a successor (this ADR).
- **Migration re-reads the JSON fixture.** Rejected: couples a frozen
  schema step to a mutable file and makes a faithful `downgrade()`
  impossible. Literal lists in the migration module are reproducible.
- **Introduce sub-letter granularity now (13a/13b, 14a/b/c, 7a/b/c, …).**
  Rejected/deferred: the statute's sub-letter breakdown is only needed
  once the reporting feature (#032) renders it. Out of scope here.

## Consequences

### Positive

- The catalogue matches § 2 Abs. 1 AnlV. The #019 Mischung evaluation
  groups against correct labels, and #032 can build its form rows on a
  trustworthy spine.
- Existing FKs and `limits.class_key` snapshots are unaffected — the fix
  is invisible to every downstream reference.
- The Öffnungsklausel and Genehmigung buckets the BerVersV forms need
  are present ahead of #032.

### Negative

- A relabel-in-place rewrites `display_name`/`description` on existing
  rows. `anlv_categories` is a global lookup table with no audit trigger
  (ADR-0057, b010), so the change provenance is the Alembic migration
  (versioned, in git), not the `audit_log`. Anyone who had memorised the
  (wrong) old labels must re-learn them.
- The migration's literal old-value list duplicates the pre-correction
  fixture content; this is the deliberate cost of a self-contained,
  reversible migration.

### Neutral / Follow-ups

- **Non-goal — test-data re-tagging (explicit).** The synthetic
  demo/test investments may still be tagged to semantically wrong AnlV
  *numbers* (e.g. a listed-equity instrument tagged `anlv_15` under the
  old "Aktien" reading, which is now OGAW). This ADR corrects the
  **catalogue**, not the **data tagged against it**. Re-tagging the
  synthetic fixtures is a separate follow-up and is also roadmap
  loose-end material (it does not block #019 or #032's catalogue
  precondition).
- **Sub-letter granularity deferred.** The statute's sub-letters
  (13a/13b, 14a/b/c, 7a/b/c, …) are deferred until the reporting feature
  (#032) needs them.
- The catalogue now holds 20 rows (18 numbered + 2 regulatory buckets)
  instead of 19; storage footprint stays negligible and never
  tenant-replicated.

## Implementation Notes

- Corrected fixture:
  `services/data_normalization/fixtures/anlv_categories.json` — the full
  statute-accurate § 2 Abs. 1 catalogue plus `anlv_oeffnungsklausel`
  (§ 2 Abs. 2) and `anlv_genehmigung` (§ 2 Abs. 3); `anlv_19` removed.
- Relabel migration:
  `db/migrations/versions/2026_06_16_1200_b018_fix_anlv_category_labels.py`
  (`down_revision = "b017_historise_composition_wts"`). `upgrade()`:
  (1) UPDATE `anlv_1`…`anlv_18` from a literal corrected list;
  (2) INSERT the two buckets `ON CONFLICT (code) DO NOTHING`;
  (3) guard-count references to `anlv_19` in `investments` and `limits`,
  then DELETE — or raise `RuntimeError` with counts + remediation.
  `downgrade()` restores the prior labels from a literal old list,
  re-INSERTs `anlv_19` (Edelmetalle, sort_order 190), and deletes the
  two buckets, each delete guarded the same way.
- Tests:
  `tests/repositories/test_anlv_category_repository.py` (AC-02 corrected
  label, AC-03 cardinality + presence of the two buckets + absence of
  `anlv_19`). `tests/test_data_normalization_fixtures.py` passes
  unchanged (it asserts shape, not labels).
- No `code` value changes, so bootstrap/seed regression tests
  (`tests/regression/test_default_tenant_seed.py`, `tests/cli`) pass
  unchanged.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Functional Correctness,
  Maintainability.
- **Regulatory references:** § 2 Abs. 1–3 Anlageverordnung (AnlV,
  18.04.2016, last amended 04.02.2026); downstream BerVersV Anlage 2
  Abschnitt C supervisory returns (#032).
- **Audit evidence:** the corrected fixture, the `b018` migration
  (with its literal upgrade/downgrade lists and the guarded delete), and
  the updated repository tests. The relabel's provenance is the Alembic
  migration itself (`anlv_categories` is a non-audited global lookup
  table); the `investments.anlv_code` references it points at remain
  audited by the b001 trigger.

## References

- ADR-0057 — AnlV Classification as 1:1 Investment Attribute (immutable
  predecessor; this ADR corrects the catalogue it introduced)
- ADR-0055 / ADR-0056 — AUM coverage engine and limit-set
  historisation (`limits.class_key` snapshots that must stay intact)
- ADR-0005 — no-silent-fallback policy (the guarded `anlv_19` delete)
- ADR-0008 — English as the sole codebase language (snake_case codes;
  German labels are operator-facing data values)
- Roadmap #019 (live Mischung evaluation affected) and #032
  (Regulatory Reporting Pre-Fill; #032a precondition satisfied here)

---

## Revision History

| Date       | Author | Change        |
|------------|--------|---------------|
| 2026-06-16 | PortfoliFLOW project owner | Initial draft, accepted |
