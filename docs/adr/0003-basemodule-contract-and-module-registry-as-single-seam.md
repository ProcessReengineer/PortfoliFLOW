# ADR-0003: BaseModule Contract and ModuleRegistry as Single Seam

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** architecture

---

## Context

PortfoliFLOW must be extensible by appending new business-logic modules without rewiring existing code. The GUI must discover modules at runtime and instantiate them uniformly, regardless of which Area they belong to or which features they expose. At the same time, every module must satisfy a minimum contract — name, area, lifecycle method, validation hook, configuration injection — so that the rest of the system can treat modules as interchangeable units.

Without a uniform contract and a single discovery point, every module would need bespoke wiring in the GUI and in any orchestration code, which contradicts the additive-extension goal recorded in ADR-0016.

## Decision

Every PortfoliFLOW module subclasses `core.base_module.BaseModule` and registers itself with the `modules.module_registry.registry` singleton (typically via the `@registry.register` decorator). The GUI discovers and instantiates modules exclusively through this registry — it never imports from individual module files.

`BaseModule` enforces the following contract at construction time:

- `module_name` must be a non-empty snake_case identifier (unique across the registry).
- `module_area` must be one of the values in `VALID_AREAS` (see ADR-0002).
- A `Settings` instance is injected through `__init__` (configuration is not read globally).
- `run(*args, **kwargs) -> dict` is abstract — every subclass must implement it.
- `validate_inputs(**kwargs)` is provided as a no-op hook subclasses may override.
- A per-module logger is set up automatically as `modules.<area>.<name>`.

## Rationale

- A single registration seam means adding a module never requires modifying the GUI's discovery code; it only requires importing the module file once so its decorator runs (see ADR-0016).
- Validating `module_area` against a fixed set at construction time prevents typos from silently routing modules to non-existent Areas.
- Injecting `Settings` through the constructor (rather than reading from a global) keeps modules unit-testable in isolation with mock configuration — a prerequisite for the Service-Layer separation in ADR-0018.
- Returning `dict` with at minimum `{"status": "ok" | "error"}` from `run()` gives the GUI a uniform way to react to module results without knowing what each module does internally.

## Alternatives Considered

- **Plugin discovery via filesystem scan (no registry):** Rejected because it makes registration order non-deterministic and complicates testing.
- **Entry-point–based discovery (`pyproject.toml [project.entry-points]`):** Rejected as over-engineered for an in-tree, single-package project.
- **Free-form interface (no abstract base class):** Rejected because nothing would prevent modules from omitting required attributes; failures would surface only at first call from the GUI.
- **Service-locator pattern with global config:** Implicitly rejected — the architecture document explicitly requires that "configuration is injected, never global".

## Consequences

### Positive

- Adding a module touches at most three existing lines (ADR-0016) because the registry is the only seam.
- The GUI can render a placeholder for any registered-but-unimplemented module without per-module code.
- Modules are unit-testable without starting PyQt6 or the rest of the application.

### Negative

- The decorator import side-effect means modules must be imported (typically via the area's `__init__.py`) to register themselves; forgetting that import silently hides the module from the registry.
- The `dict`-typed return value is a weak contract; richer typing (e.g., `TypedDict` per module) would catch more mistakes statically.

### Neutral / Follow-ups

- Consider tightening the `run()` return type with `TypedDict` or per-module result classes once the number of modules grows.
- Consider an automated check that every file under `modules/<area>/` is imported by its `__init__.py`, to catch forgotten registrations.

## Implementation Notes

- Base class: `core/base_module.py` (`BaseModule`, `VALID_AREAS`).
- Registry: `modules/module_registry.py` (`ModuleRegistry`, `registry` singleton).
- Example registered module: `modules/front_office/data_import.py`.
- GUI consumption: `gui/main_window.py` (uses `registry.get(...)` and `registry.list_by_area(...)`).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modularity, modifiability), Testability.
- **Audit evidence:** Code inspection — every registered module appears in `registry.all()`; every module file imports `BaseModule` and `registry`.

## References

- ADR-0001 (Layered architecture and strict one-way dependencies)
- ADR-0002 (Canonical glossary)
- ADR-0016 (Module-scope rule)
- `docs/architecture.md`, principle #4: "The registry is the single seam."

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code and documentation; the original decision predates this ADR. |
