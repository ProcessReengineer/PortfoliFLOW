# ADR-0016: Module-Scope Rule — Adding a Module Touches at Most Three Existing Lines

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** process, architecture

---

## Context

PortfoliFLOW grows by adding Modules. With AI assistance, a "small" change can become a sprawling diff that incidentally restructures unrelated files. Even with all the structural rules in place (layered architecture, registry, BaseModule contract), the project needs a single, simple, mechanical check that a "new module" change is in fact a *new module* change.

The check has to be expressible without running the code, has to be easy to apply in code review, and has to be hard to game.

## Decision

Adding a new Module to PortfoliFLOW must modify at most **three lines** in existing files:

1. One import line in the area's `__init__.py` (so the module file is imported and its `@registry.register` decorator runs).
2. One `registry.register(...)` call in `modules/module_registry.py` *or* the use of the decorator on the new module class (which means this line lives in the new module file, not in an existing one).
3. One entry in the GUI sidebar list for the relevant Area, *if and only if* the new module is meant to appear as a sidebar entry. (For modules whose UI is embedded in an Area Panel via the mini-nav pattern, this third change is replaced by editing the relevant Panel — counted the same way.)

Everything else introduced by the change must be in **new files**. If a change to an existing file beyond these three lines is necessary, the author stops and reconsiders the design.

## Rationale

- A single, mechanical budget makes the additive-extension philosophy enforceable at code-review time.
- The rule maps directly to the architecture: the registry seam (ADR-0003), the area-by-directory layout (ADR-0001 / ADR-0002), and the panel-per-area GUI structure.
- Any AI-generated change that breaks the budget is, by construction, doing something other than adding a Module — and is therefore worth a manual second look.
- The three-line budget is small enough to remember without consulting documentation.

## Alternatives Considered

- **No budget; rely on review judgment:** Rejected — without a number, "a small change" is unbounded; AI diffs routinely creep beyond what a single reviewer can hold in mind.
- **Per-area budgets or per-file budgets:** Rejected as over-specified; the three-line aggregate captures what matters.
- **Allow refactoring of existing files alongside module additions:** Rejected for the additive case — refactors that have to happen are valid work, but they belong in their own commit and ADR-style review, not bundled into a "new module" change.

## Consequences

### Positive

- Clean reviewability of "new module" diffs.
- AI prompts can include the budget as a hard constraint.
- Misplaced abstractions surface early ("I cannot add this module without touching X" → X is in the wrong place).

### Negative

- Genuine cross-cutting changes (e.g., introducing a new Area) are intentionally not covered by this rule and require separate planning.
- Authors may try to fit work into the budget by hiding edits in the new module file; review must still check that the new file is itself well-scoped.

### Neutral / Follow-ups

- The rule could be partly mechanised by a CI check that compares the line-touch count for files matching `modules/<area>/__init__.py`, `modules/module_registry.py`, and `gui/main_window.py` against the diff in PRs.
- If the GUI sidebar structure changes (e.g., dynamic discovery from the registry), revisit the third entry in the budget.

## Implementation Notes

- Documented in: `CLAUDE.md` ("The one rule that matters most"), `docs/architecture.md` (principle #3 "New functionality is always additive").
- Embodied in: `modules/<area>/__init__.py` files; `modules/module_registry.py`; `gui/main_window.py` and `gui/panels/`.
- Module spec input to AI prompts: `docs/module_spec_template.md`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modifiability — changes are bounded), Reliability (low risk of incidental regression on additive changes).
- **Audit evidence:** Diff inspection on a "new module" PR; commit history shows additive-only patterns for module additions.

## References

- ADR-0001 (Layered architecture)
- ADR-0002 (Canonical glossary — defines "Module" / "Area" / "Panel")
- ADR-0003 (BaseModule contract and ModuleRegistry as single seam)
- ADR-0015 (Claude-assisted workflow — module-implementation sessions are held to this budget)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing documentation; the rule predates this ADR. |
