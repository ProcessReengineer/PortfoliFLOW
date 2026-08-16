# ADR-0005: Typed Exception Hierarchy Rooted in PortfoliFlowError

- **Status:** Superseded by ADR-0044
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** architecture, process

---

## Context

PortfoliFLOW spans data import, configuration, business modules, GUI presentation, and external services. Errors originating in any of these layers must be distinguishable by callers (e.g., the GUI should show a user-friendly dialog for an `DataImportError` but log a stack trace for an unexpected `ModuleError`). At the same time, callers that do not need to discriminate should be able to catch a single base type.

A consistent exception strategy is also a precondition for AI-generated code: prompts can specify "raise `ValidationError` from `core.exceptions`" without ambiguity, and reviewers can mechanically check that no `Exception` is raised directly.

## Decision

Every PortfoliFLOW exception subclasses `core.exceptions.PortfoliFlowError`. The current hierarchy is:

- `PortfoliFlowError` (base)
  - `ConfigurationError` — invalid or missing configuration.
  - `DataImportError` — import / parsing failures from external data.
  - `ValidationError` — module input validation failures (carries optional `field` attribute).
  - `ModuleError` — unexpected runtime failure inside a module.
  - `ServiceError` — failure in an external service integration (AI, storage, …).

Code rules (enforced by review and `CLAUDE.md`):

- Never raise the bare `Exception` class.
- Never catch bare `Exception` without re-raising or logging.
- New exception types must subclass an appropriate level of this hierarchy.

## Rationale

- A single base class lets generic error-handling code (e.g., the GUI's top-level handler, or a future telemetry layer) catch every application-defined error with one `except`.
- Distinct subclasses give specific call sites the option to react differently (retry, surface to user, suppress) without inspecting messages.
- Forbidding bare `Exception` raises makes failures discoverable and prevents the "swallowed exception" anti-pattern.
- The five-subclass split mirrors the five-layer architecture (config, data, modules, services) plus a cross-cutting validation type — minimal but sufficient.

## Alternatives Considered

- **No custom exceptions; raise stdlib types (`ValueError`, `RuntimeError`):** Rejected because it makes it impossible to distinguish PortfoliFLOW-originated errors from third-party library errors at catch sites.
- **Per-module exception classes:** Rejected as premature — the current five categories cover all real call sites; per-module types can be added later as subclasses if needed.
- **Result/Either return types instead of exceptions:** Rejected — Python idiom and the `pandas` / `scipy` ecosystem use exceptions; introducing a `Result` type would create churn at every boundary.

## Consequences

### Positive

- Consistent error semantics across layers; one base type to catch.
- AI-generated code has an unambiguous answer to "which exception should I raise".
- Carry-extra-context fields (e.g., `ValidationError.field`) can be added per subclass without breaking existing handlers.

### Negative

- Authors of new exception types must remember to subclass `PortfoliFlowError` rather than `Exception`.
- The hierarchy may grow over time; periodic pruning will be needed to keep it useful.

### Neutral / Follow-ups

- Consider adding `DataVaultError` once ADR-0017's DataVault is implemented.
- Consider a ruff rule (`TRY002`-style) or custom check that flags `raise Exception(...)` and `except Exception:` without re-raise/log.

## Implementation Notes

- Implementation: `core/exceptions.py`.
- Used throughout: `core/`, `services/`, `modules/`, `analytics/`.
- Documented in: `CLAUDE.md` ("Error handling").

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Reliability (fault tolerance), Maintainability (analysability — error origins are typed).
- **Audit evidence:** Source inspection of `core/exceptions.py`; grep for `raise Exception(` / `except Exception:` should return only the documented exceptions (e.g., the deliberate broad catch in `services/tool_registry.py`).

## References

- ADR-0001 (Layered architecture)
- ADR-0003 (BaseModule contract — `validate_inputs` raises `ValidationError`)
- ADR-0012 (ToolRegistry — uses a documented broad-catch in `execute_tool`)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code and documentation; the original decision predates this ADR. |
| 2026-05-06 | PortfoliFLOW project owner | Status changed to *Superseded by ADR-0044*. The exception class identifier was renamed from `PortfolioFlowError` to `PortfoliFlowError` as part of the project-name unification (`portfolioflow` → `portfoliflow`). The hierarchy and policy described in this ADR remain in force; only the identifier changes. See ADR-0044 for the rename rationale. |
