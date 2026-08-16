# ADR-0001: Layered Architecture and Strict One-Way Dependencies

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** architecture

---

## Context

PortfoliFLOW is built and maintained by a single developer working with AI assistance. Every change must be reviewable in a focused session, and AI-generated code must not silently re-wire how parts of the system depend on each other. Without an explicit dependency contract, even small additions can introduce circular imports, hidden coupling between unrelated business areas, or business logic leaking into the GUI — all of which are expensive to undo once they are merged.

The project also targets institutional use cases (fund-of-funds back office, investor reporting, regulated workflows). For these contexts, traceability of *who depends on what* is part of the audit story (separation of concerns, change-impact analysis).

## Decision

PortfoliFLOW is organised into five named layers — `analytics/`, `core/`, `services/`, `modules/`, `gui/` — with import directions strictly fixed in one direction. Sibling-to-sibling imports inside `modules/` are forbidden. Circular imports are treated as design errors, not as something to work around with lazy imports.

The allowed import graph is:

```
GUI  →  ModuleRegistry  →  Modules  ←  core/ (incl. DataStore)
                                    ←  services/
                                    ←  analytics/
GUI  →  analytics/ (widgets may call analytics engines directly)
GUI  →  services/ (widgets may call services directly)
analytics/  ←  core/ (exceptions only)
```

Concretely:

- `core/` imports nothing from inside the project.
- `services/` imports from `core/` only.
- `analytics/` imports from `core/` (exceptions only) plus third-party packages.
- `modules/` imports from `core/`, `services/`, and `analytics/` — never from sibling modules.
- `gui/` imports from `modules/module_registry`, `core/`, `services/`, and `analytics/` — never from individual module files.

## Rationale

- A small number of named layers, each with a single responsibility, is the cheapest way to keep a growing codebase reviewable for one developer plus AI.
- Forbidding sibling imports inside `modules/` makes each module independently replaceable — a hard requirement for the additive-module workflow recorded in ADR-0016.
- Putting `analytics/` outside `modules/` — and depending only on `core.exceptions` — means computational engines can be reused by any module, by GUI widgets, and by AI tools without dragging in DataStore or GUI dependencies (see ADR-0013).
- Treating circular imports as design errors (rather than papering over them with `import` statements inside functions) preserves the value of the layering: a lazy import is a circular dependency that has been hidden, not removed.

## Alternatives Considered

- **Flat structure (no layers):** Rejected because it removes any structural barrier to coupling between unrelated areas; AI-generated diffs become unbounded.
- **Hexagonal / ports-and-adapters per module:** Rejected as over-engineered for the project's current size. The layered approach captures the same separation with less ceremony.
- **Allowing lazy imports to break cycles:** Implicitly rejected — explicitly forbidden in `CLAUDE.md`. A cycle is evidence that the layering is wrong; hiding it makes the underlying design problem invisible.
- **Status quo (no documented dependency rules):** Rejected — without explicit rules, AI code-generation prompts have no guardrail against introducing forbidden imports.

## Consequences

### Positive

- AI-generated changes can be reviewed by checking that imports point in the allowed direction.
- New modules cannot accidentally couple to existing modules.
- The GUI can be tested independently of business logic, and business logic can be tested without instantiating PyQt6.
- Future migration to a client-server topology (see ADR-0018) is easier because the layers already exist.

### Negative

- Some duplication is accepted across modules rather than reaching for a shared sibling import.
- Genuine cross-module collaboration must be routed through `core/` (typically the DataStore — see ADR-0004) or through `services/`, which adds indirection.
- Reviewers (human and AI) must hold the dependency rules in mind on every change.

### Neutral / Follow-ups

- A static check (e.g., via `import-linter` or a custom ruff rule) could mechanise enforcement; currently enforcement is by review.
- The PyQt6 import inside `services/ai_service.py` is an explicit, documented exception — captured separately in ADR-0011.

## Implementation Notes

- Documented in: `CLAUDE.md` ("Dependency rules"), `docs/architecture.md` ("Dependency rules", "Layer responsibilities").
- Embodied in: directory structure under `analytics/`, `core/`, `services/`, `modules/`, `gui/`.
- Acknowledged exception: `services/ai_service.py` (top-of-file architecture note).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modularity, modifiability, analysability), Portability.
- **Audit evidence:** Code inspection of import statements per layer; the `CLAUDE.md` rules are part of the AI-collaboration contract.

## References

- ADR-0003 (BaseModule contract and ModuleRegistry as single seam)
- ADR-0011 (Acknowledged PyQt6 dependency in AIService)
- ADR-0013 (Analytics layer: pure, stateless)
- ADR-0016 (Module-scope rule)
- ADR-0018 (Planned Service / Repository layering)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing documentation and code; the original decision predates this ADR. |
