# ADR-0018: Planned Service / Repository Layering as Prerequisite for Client-Server Migration

- **Status:** Accepted (initial implementation in Strang B of Phase 1)
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** architecture, process

---

## Context

PortfoliFLOW currently runs as a single-user PyQt6 desktop application. Some business logic is already cleanly separated (e.g., `analytics/portfolio_optimizer.py` per ADR-0013, `services/ai_service.py` per ADR-0010), but in places computational logic still sits inside `QWidget` subclasses or directly inside Module GUI integrations. As the project anticipates a future client-server topology (FastAPI backend with browser or desktop frontend) and a multi-user mode (ADR-0019), the boundary between presentation and business logic must be made explicit before the persistence layer (ADR-0017 — DataVault) is wired in.

Without this separation, every persistence call would inevitably grow GUI dependencies, and a later client-server migration would be a rewrite rather than a refactor.

## Decision

PortfoliFLOW will adopt a four-layer separation as the target architecture:

1. **UI Layer** (PyQt6) — display and user-event handling only.
2. **Service Layer** — business logic (e.g., IRR calculation, scraping orchestration, report generation). Plain Python; callable without instantiating the UI.
3. **Repository Layer** — all DataVault read/write operations. Each repository encapsulates a domain (funds, transactions, NAVs, …) and returns plain DTOs / dataclasses.
4. **DataVault** (DuckDB; see ADR-0017) — physical persistence.

The heuristic for whether code lives in the right layer: *"Could I call this function in a unit test without starting the PyQt6 application?"* If the answer is "no", the code belongs in a Service or a Repository, not in the UI class.

This ADR is `Proposed` because the Service / Repository layers are not yet implemented; the heuristic is being applied incrementally as code is touched. An architecture-review pass per ADR-0015 will run before the DataVault implementation begins, with the explicit focus of identifying remaining UI-embedded logic to extract.

## Rationale

- A clean Service Layer is the prerequisite for any non-Qt frontend (web, CLI, or future agent-driven entry point).
- A Repository Layer encapsulates persistence so the rest of the system depends on domain DTOs, not on DuckDB types — making the persistence engine itself swappable later.
- Doing the separation *before* DataVault wiring is far cheaper than retrofitting it afterwards: every wire that crosses the UI/persistence boundary directly is a future refactor.
- The four-layer split aligns with how the project already structures code (`analytics/`, `services/`, `modules/`, `gui/`) and extends it rather than replacing it.

## Alternatives Considered

- **Skip the separation; let modules call DataVault directly:** Rejected — would couple every module to DuckDB and to the persistence schema, defeating ADR-0017's modifiability goal.
- **Introduce the Service / Repository layers only when client-server migration is started:** Rejected — by then the UI-embedded logic will be larger, and the migration becomes a rewrite.
- **Adopt a heavier framework (Django-style ORM, full hexagonal architecture per module):** Rejected as over-engineered for the current size; the four-layer split captures the necessary separation without ceremony.

## Consequences

### Positive

- Future client-server migration becomes a refactor (introduce a transport layer above Services) rather than a rewrite.
- Business logic is unit-testable without PyQt6.
- Persistence engine remains swappable behind the Repository façade.

### Negative

- Adds indirection for some currently-direct GUI→logic call paths.
- Existing UI-embedded logic must be incrementally extracted; until that work is done, the layering is partly aspirational.
- More files per Feature (Service + Repository + UI) than today.

### Neutral / Follow-ups

- The architecture-review pass scheduled before DataVault implementation (per ADR-0015) is the right point to enumerate remaining UI-embedded logic by file.
- Each Module's Widget should be checked against the heuristic over time.
- Naming convention for Service / Repository classes (e.g., `*_service.py`, `*_repository.py`) is a small follow-up decision.

## Implementation Notes

- Documented in: `CLAUDE.md` ("Separation of Concerns") and `docs/architecture.md` ("What does not belong here" — repository pattern reference).
- Existing examples that already follow the pattern: `analytics/portfolio_optimizer.py` (pure logic, no UI), `services/ai_service.py` (singleton service).
- Existing examples that need extraction: places where compute / I/O sits inside `gui/widgets/*.py` instead of in `analytics/` or a Service.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modularity, testability), Portability (frontend swap), Compatibility.
- **Audit evidence (once implemented):** Imports inside `gui/` reference Services / Repositories, not persistence directly; unit tests cover Services without `QApplication`.

## References

- ADR-0001 (Layered architecture)
- ADR-0010 (AIService singleton)
- ADR-0013 (Analytics layer pure and stateless — exemplar of the target separation)
- ADR-0015 (Claude-assisted workflow — defines the architecture-review pass)
- ADR-0017 (Planned DataVault — the persistence engine the Repository Layer will hide)
- ADR-0019 (Planned multi-user readiness)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from "Planned Architecture" notes in `CLAUDE.md`; partial implementation. |
| 2026-04-29 | PortfoliFLOW project owner            | Cross-reference note: ADR-0029 has delivered the first Qt-free service entry point (`services/headless_shirley.py`, Shirley's turn loop), partially anticipating the service / repository split this ADR describes. The remaining work — Repository Layer per domain, separation of UI-embedded computation, and the eventual transport layer above Services — is unchanged. |
| 2026-05-03 | PortfoliFLOW project owner            | Status moved to **Accepted (initial implementation in Strang B of Phase 1)**. The repository layer landed under `core/repositories/` with `BaseRepository`, an async `tenant_context` context manager (the only sanctioned way to obtain a tenant-scoped session, per ADR-0035 §4), and `UserRepository` as the first concrete consumer. Repositories return frozen `UserDTO`-style dataclasses, never SQLAlchemy ORM instances — the domain layer remains ignorant of SQLAlchemy lifecycle as ADR-0034 §3 requires. Full layering across all existing modules (Excel import, SAA, optimisation, reporting) is Phase 2/3 work as the FastAPI surface and per-area migrations land. Decider: PortfoliFLOW project owner. |
