# ADR-0008: English as the Sole Codebase Language

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** process

---

## Context

PortfoliFLOW is developed by a German-speaking team and operated in a market where some input data (Excel sheets, GP reports, fund documentation) is in German. There is therefore a real temptation to use German identifiers, comments, or docstrings in the codebase. Mixing languages — even within a single file — has well-known costs: inconsistent vocabulary, harder onboarding, and weaker results from AI assistants whose pretraining is dominated by English source code.

A clear language convention is also part of making the codebase reviewable by future contributors and external auditors who are not necessarily German speakers.

## Decision

All code (identifiers, comments, docstrings, log messages, exception messages, README, ADRs, in-repo documentation) is written in English. Input data values may remain in their source language — for example, German labels such as `Aktien`, `Private Equity`, `Typ der Investition`, or `Klasse der Investition` are valid string content of imported Excel sheets and are recognised as data by the import code.

User-facing GUI strings are currently in English. Internationalisation of the UI is out of scope for this ADR.

## Rationale

- English is the de-facto language of the Python ecosystem, library documentation, and the AI assistants generating code in this project; matching that language reduces friction.
- Keeping data values in their source language means the import path does not silently translate and so does not destroy traceability between the spreadsheet and the imported DataFrame.
- A single code language eliminates one whole category of inconsistency review feedback.

## Alternatives Considered

- **German for everything:** Rejected — would degrade AI-generated code quality and make the codebase opaque to non-German-speaking reviewers (including auditors).
- **Bilingual (German for business terms, English for technical):** Rejected — the boundary is impossible to police, and ambiguous terms ("Position", "Reporting") would constantly migrate between the two.
- **English code, German user-facing strings via i18n today:** Rejected as premature — UI internationalisation can be added later without changing the code-language decision.

## Consequences

### Positive

- Consistent reading experience for any reviewer.
- Better-quality AI-generated code and review.
- Future i18n of user-facing strings becomes a clean, scoped change.

### Negative

- German-speaking authors must translate domain vocabulary in their head; some German terms ("Gesellschafterversammlung", "Kapitalabruf") have no perfectly compact English equivalent.
- A future requirement to localise the GUI will require introducing an i18n layer (gettext / Qt linguist).

### Neutral / Follow-ups

- A glossary of German↔English term mappings could help if domain vocabulary becomes ambiguous in code; not yet warranted.
- Localisation of GUI strings is a candidate for a future ADR if and when it is required.

## Implementation Notes

- Convention enforced by review.
- Observable in: every source file under `core/`, `services/`, `modules/`, `analytics/`, `gui/`, plus all documentation under `docs/` and `readme.md`.
- Counter-examples (data, not code): German strings inside `Attributes` and time-series sheets handled by `modules/front_office/data_import.py`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (analysability for non-German reviewers).
- **Audit evidence:** Source inspection.

## References

- ADR-0009 (Excel V2 import format — preserves source-language data values)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from observed convention; the convention predates this ADR. |
