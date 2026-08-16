# ADR-0007: Google-Style Docstrings on All Public APIs

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** process

---

## Context

PortfoliFLOW is built with AI assistance and intended to remain reviewable by a single developer over time. Docstrings serve three audiences: human reviewers, AI assistants generating subsequent code, and any future documentation generator (e.g., MkDocs / Sphinx). A consistent docstring style is what makes those three uses cheap; a mixed style means each reader has to disambiguate every entry.

PortfoliFLOW also documents *why* code exists, not only *what* it does — a property the docstring style must support without forcing a heavy tool chain.

## Decision

All public classes, methods, and functions in PortfoliFLOW use Google-style docstrings. Where applicable, docstrings include the standard sections:

- `Args:` — every parameter, with a one-line description.
- `Returns:` — the return value's shape and meaning.
- `Raises:` — every exception explicitly raised by the function.
- `Example::` (RST-style literal block) — for non-trivial APIs.

One-line docstrings are acceptable only for trivial properties (e.g., simple accessors). Module-level docstrings are required and explain purpose, usage, and invariants relevant to the file as a whole.

## Rationale

- Google style is compact and readable in the source file, unlike NumPy-style which is more verbose, and unlike reStructuredText field lists which are noisier per parameter.
- The `Args` / `Returns` / `Raises` triple maps cleanly to Python's behaviour and is well-supported by every documentation generator and IDE.
- A single style across the codebase removes one degree of freedom from every code-review and code-generation prompt.
- Module-level docstrings give AI assistants the high-level "what does this file do" context without requiring them to read the whole module.

## Alternatives Considered

- **NumPy-style docstrings:** Rejected — equally expressive but more vertical space per signature; the codebase has many short utilities where this becomes noisy.
- **reStructuredText field lists (`:param:` / `:returns:`):** Rejected — denser markup, harder to skim in source.
- **No required style:** Rejected — mixed styles increase cognitive load on every review.
- **Generated docs only (no in-source docstrings):** Rejected — the developer reads source far more often than generated docs; in-source documentation is where the value sits today.

## Consequences

### Positive

- Consistent reading experience across modules.
- AI assistants can be prompted to produce docstrings in a single style without arbitration.
- Compatible with future MkDocs/Sphinx adoption (Google style is supported by both via `napoleon` / native plugins).

### Negative

- Docstrings can drift from code if not updated alongside changes; review discipline is required.
- One-line docstrings are not always sufficient even when the prose is short — authors must judge when `Args` / `Returns` / `Raises` are warranted.

### Neutral / Follow-ups

- A docstring linter (e.g., `pydocstyle`, or a `ruff` rule subset) is not yet enabled. Consider adopting once the API surface stabilises.
- Generated documentation (MkDocs / Sphinx) is not in scope today; the docstrings are written to support it later.

## Implementation Notes

- Documented in: `CLAUDE.md` ("Code conventions").
- Visible throughout: `core/`, `services/`, `analytics/`, `modules/` — especially the public classes (`BaseModule`, `DataStore`, `ModuleRegistry`, `AIService`, `ToolRegistry`, `PortfolioOptimizer`).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (analysability — documentation co-located with code).
- **Audit evidence:** Source inspection; a future automated docstring check would generate an audit trail.

## References

- ADR-0006 (Python 3.11+ and modern type syntax — type hints + docstrings together describe the contract)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code; the original decision predates this ADR. |
