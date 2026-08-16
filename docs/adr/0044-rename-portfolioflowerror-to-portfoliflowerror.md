# ADR-0044: Rename `PortfolioFlowError` to `PortfoliFlowError` for Project-Name Unification

- **Status:** Accepted
- **Date:** 2026-05-06
- **Deciders:** PortfoliFLOW project owner
- **Tags:** process, architecture

---

## Context

The base exception class introduced by ADR-0005 carried the identifier
`PortfolioFlowError` — with a redundant second "o" inherited from the
obsolete project-name spelling `portfolioflow`. The canonical project
and package name is `portfoliflow` (no second "o"); the marketing-style
prose form is `PortfoliFLOW`. The mismatch between the package name and
the exception identifier was a single residual stretch of the older
spelling, and the only place the obsolete form survived inside Python
identifiers.

A separate naming-unification pass (same commit) updates the
`pyproject.toml` distribution name, the GUI console-script entry, the
DataVault path defaults in documentation, and a stale repository-name
reference in ADR-0039. This ADR records the corresponding identifier
rename for the exception base class so the change is discoverable in
the ADR log rather than buried in a single refactor commit.

## Decision

The base exception class is renamed:

```
PortfolioFlowError  →  PortfoliFlowError
```

All subclasses (`ConfigurationError`, `DataImportError`, `ValidationError`,
`ModuleError`, `ServiceError`, `ToolRegistryError`, `AllowlistError`,
`FetchError`, `ExtractionError`, `FeedParseError`) and all `raise` /
`except` sites are migrated to the new identifier in the same commit.

No backwards-compatibility alias is introduced. The exception class is
internal to PortfoliFLOW; there are no third-party consumers that import
it across a stable API boundary.

ADR-0005 is updated to status `Superseded by ADR-0044`. Its content
(the hierarchy and the code rules) remains in force — only the
identifier changes. The ADR-0005 title is updated in lockstep so that
file-tree searches for the new identifier succeed; the original
identifier remains discoverable via the Revision History table.

## Rationale

- A single canonical project name across distribution, console scripts,
  paths, Postgres roles, logger names, and Python identifiers reduces
  cognitive load and prevents AI-generated code from picking up the
  obsolete spelling.
- A hard rename (no alias) is preferred because the identifier surface
  is fully internal: every importer is in this repository and was
  updated atomically.
- ADR-0005's status is changed to *Superseded* rather than *Amended*
  because the identifier change is API-visible to anyone reading the
  earlier ADR, even though the policy itself is unchanged. Marking it
  *Superseded* aligns with the precedent set by ADR-0017 →
  ADR-0034.

## Alternatives Considered

- **(B) Rename with deprecation alias.** Rejected. The exception class
  has no third-party consumers; carrying a deprecated alias plus
  `DeprecationWarning` adds maintenance burden with no migration
  population to serve.
- **(C) Status quo — keep `PortfolioFlowError`, document the drift in a
  glossary.** Rejected. A glossary note is a soft constraint that
  AI-generated code routinely violates by inferring the obsolete
  spelling from the identifier itself.

## Consequences

### Positive

- The codebase contains a single canonical spelling of the project name
  in all identifier-bearing contexts (package, console scripts, paths,
  exception class, logger names).
- ADR-0005's policy survives unchanged; only the identifier moves.

### Negative

- A single large commit touches every `raise PortfolioFlowError(...)`
  and `except PortfolioFlowError as exc:` site. Mitigated by the fact
  that the rename is mechanical and the diff is dominated by
  identifier changes.

### Neutral / Follow-ups

- Out-of-tree consumers (e.g. local notebooks the developer keeps
  outside the repository) that imported `PortfolioFlowError` will
  break on next pull — by design.
- A canonical-spelling glossary entry is added to `CLAUDE.md` to
  prevent future drift.

## Implementation Notes

- Identifier rename applied to: `core/exceptions.py`, `core/__init__.py`,
  `services/tool_registry.py`, `services/web_research/allowlist.py`,
  `services/web_research/fetcher.py`, `cli/bootstrap.py`,
  `cli/reset_dev.py`.
- Documentation updates: `CLAUDE.md`, `docs/architecture.md`,
  ADR-0005 (title, body, Revision History), ADR-0022 cross-reference,
  `docs/adr/README.md` index.
- The same commit unifies `portfolioflow` → `portfoliflow` in
  `pyproject.toml`, `readme.md`, `.env.example`, `CLAUDE.md`,
  ADR-0000, ADR-0017, and ADR-0039.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (consistency of identifiers across the codebase).
- **Audit evidence:** `git grep PortfolioFlowError` should return no
  results in source files; the term remains only in the Revision
  History tables of ADR-0005 and ADR-0044.

## References

- ADR-0005 (superseded by this ADR; identifier rename is the only
  substantive change)
- `CLAUDE.md` Glossary entry *Project name spellings*

---

## Revision History

| Date       | Author                          | Change                                                                 |
|------------|---------------------------------|------------------------------------------------------------------------|
| 2026-05-06 | PortfoliFLOW project owner      | Initial draft. Records the `PortfolioFlowError` → `PortfoliFlowError` rename and supersedes ADR-0005. |
