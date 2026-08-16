# ADR-0004: In-Memory DataStore Singleton with Documented Extension Path

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** architecture, data

---

## Context

PortfoliFLOW modules in different Areas need to share data. For example, time-series imported by Front Office's `data_import` must be available to Back Office and Assistants modules without those modules importing the importer (forbidden by ADR-0001). At the same time, the project is not yet ready to commit to a persistence technology (DuckDB is planned — see ADR-0017 — but not yet implemented), and persistence is not required for the current single-user, single-session workflow.

A mechanism is required that (a) provides a uniform place for cross-module data exchange, (b) does not couple modules to one another, and (c) can later be replaced with a persistent backend without rewriting consumers.

## Decision

PortfoliFLOW provides a process-wide singleton `DataStore` (in `core/data_store.py`) that holds named `pandas.DataFrame` objects in memory. Modules read and write through the public API: `store(name, df, metadata=None)`, `get(name)`, `get_metadata(name)`, `list()`, `remove(name)`, `clear()`. Both `store()` and `get()` deep-copy DataFrames at the boundary so callers cannot mutate stored data.

The singleton is obtained via `get_data_store()`, mirroring the `get_config()` pattern used elsewhere in `core/`. Persistence is not in scope for this ADR; the extension path (subclass `DataStore`, override storage methods, swap in via the factory) is documented in the module docstring.

## Rationale

- A singleton in `core/` lets modules in different Areas share data without violating the no-sibling-imports rule (ADR-0001): producers and consumers both depend on `core/`, never on each other.
- Storing copies on both `store()` and `get()` makes the API's mutation semantics explicit and protects shared state from accidental in-place edits in consumers.
- Holding data in memory is correct for the current desktop, single-session workflow. Persistent storage is a separate decision (ADR-0017 — DataVault) with its own constraints.
- Keeping the public API stable while the backend may change later means the cost of adopting persistence is bounded to the `DataStore` class itself.

## Alternatives Considered

- **Module-level globals or class attributes for cross-module data:** Rejected because they couple consumers to producers (the consumer must import the producer's module) and re-introduce the sibling-import problem.
- **Pass DataFrames explicitly through method calls:** Rejected for cross-Area sharing; it would force the GUI or an orchestrator to know which module produced which dataset, which is a coupling the registry pattern (ADR-0003) was designed to avoid.
- **Adopt a persistent store immediately (DuckDB / SQLite):** Rejected as premature — the data model, ownership, and audit-field requirements are still being designed; a documented extension path (see ADR-0017) is sufficient for now.
- **Event bus / pub-sub:** Rejected as over-engineered for a single-process desktop app where modules can pull data on demand.

## Consequences

### Positive

- Cross-Area data sharing without inter-module imports.
- Uniform debugging surface — the Debug Data Window enumerates all datasets through `list()`.
- Consumers see a stable API; persistence can be added later by subclassing.

### Negative

- All data is lost when the application closes; users must re-import on every session.
- Memory pressure scales with the size of imported workbooks; no spill-to-disk.
- A defensive copy is taken on both `store()` and `get()`, costing memory and CPU compared with shared references.

### Neutral / Follow-ups

- ADR-0017 captures the planned DuckDB-backed persistent layer (DataVault) and the audit fields required of it.
- Consider adding a maximum-size guard or eviction policy if memory pressure becomes a problem before DataVault lands.

## Implementation Notes

- Implementation: `core/data_store.py` (`DataStore`, `get_data_store`).
- Producer example: `modules/front_office/data_import.py` (calls `store.store(name, df, metadata=...)`).
- Debug surface: `gui/debug_data_window.py` (uses `store.list()`).
- Documented extension path: docstring at the top of `core/data_store.py`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modifiability — backend swap is encapsulated), Reliability (data is not persisted, so durability guarantees are zero by design until ADR-0017 is implemented).
- **Audit evidence:** Source code of `core/data_store.py` and the `Debug Data Window` view.

## References

- ADR-0001 (Layered architecture)
- ADR-0017 (Planned DataVault: DuckDB-backed persistent layer with audit fields)
- ADR-0018 (Planned Service / Repository layering)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code and documentation; the original decision predates this ADR. |
