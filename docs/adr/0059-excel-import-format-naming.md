# ADR-0059: Excel Import Format — Naming Hygiene

- **Status:** Accepted
- **Date:** 2026-05-20
- **Deciders:** PortfoliFLOW project owner
- **Tags:** process, naming, excel-import, language-hygiene

---

## Context

The Excel multi-sheet import format introduced in Phase 0 was
labelled "V2" — a reference to a redesigned format that replaced an
earlier exploratory format. After Phases 1–6, the original "V1"
format is gone from the codebase entirely. The "V2" label, once
informative, now signals a version axis that has only one value and
implies a future "V3" that is not on any roadmap.

The misleading naming was identified during the 2026-05-20
documentation audit. Three concrete consequences motivated the
rename:

1. Documentation prose ("the V2 spec", "the V2 workbook", "the V2
   sample") asks the reader to track a version dimension that does
   not exist.
2. AI-assisted development sessions consume the term and reproduce
   it in new code, perpetuating the misleading vocabulary.
3. Future external documentation, pitches, or audits exposed to the
   term would face the same question: "what was V1, and is V3
   coming?"

At the same time, the persisted `data_uploads.format_version`
column carries the literal value `"v2"`. Renaming this database
identifier would require a data migration and would break audit-
trail traceability for every existing import record.

## Decision

The user-facing and developer-facing language for the Excel import
format changes from "V2" to "Excel import format" (or "Excel import
file", as natural in context). The persisted database identifier
`format_version = "v2"` is left unchanged.

Concretely:

- **In prose, docstrings, comments, error messages, user-facing
  strings, and test fixture identifiers:** the format is named
  "Excel import format" or "Excel import file".
- **In the database** (`data_uploads.format_version` column):
  the value remains `"v2"`. New imports continue to write this
  literal. The DB identifier is an immutable historical handle.
- **In ADRs already published before 2026-05-20** (ADR-0009 in
  particular): the historical "V2" language is preserved. ADRs are
  immutable historical records; renaming inside them would erase
  the chronology of the decision.

The format itself is unchanged. Only the language used to refer to
it changes.

## Rationale

- The format is *one* format. Naming it without a version number
  matches the actual artefact.
- ADR-0009 remains the authoritative format specification.
  Referring readers to "the Excel import format (per ADR-0009)" is
  unambiguous and discoverable.
- Preserving `format_version = "v2"` keeps the audit-trail
  reproducible and avoids a non-substantive data migration.
- The precedent for documenting a naming change as a dedicated ADR
  is ADR-0044 (PortfolioFlowError → PortfoliFlowError).

## Alternatives Considered

- **Rename the database identifier too.** Rejected. The cost (data
  migration, audit-trail break, no functional benefit) exceeds the
  cost of a one-paragraph mental footnote ("the DB calls it v2, the
  prose calls it the Excel import format").
- **Coin a versioned name with semantic content.** Rejected as
  premature: there is no concrete plan for a successor format, and
  inventing a version axis pre-emptively reproduces the original
  problem.
- **Do nothing.** Rejected. The drift between term and reality
  generates ongoing friction.

## Consequences

### Positive

- Documentation reads naturally without spurious version references.
- New contributors are not confused by a phantom V1.
- AI-assisted code generation learns the correct vocabulary.

### Negative

- A one-time rename pass across ~25 files (executed in task P7).
- A small, persistent split between in-prose language and the DB
  identifier. This is documented here and called out in the
  clarifying comment in `web/routes/data_import.py`.

### Neutral

- The format itself is unchanged. ADR-0009 remains the
  specification of record.

## Compliance and audit relevance

**Low.** The decision affects naming, not behaviour or persisted
data. The audit trail of past imports is fully preserved by the
database identifier.

## Revision History

| Date       | Author                     | Note                  |
|------------|----------------------------|-----------------------|
| 2026-05-20 | PortfoliFLOW project owner | Initial draft (Accepted). |
