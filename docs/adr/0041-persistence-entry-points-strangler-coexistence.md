# ADR-0041: Persistence Entry-Points — Strangler-Coexistence of In-Memory and Postgres

- **Status:** Accepted
- **Date:** 2026-05-04
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, persistence, strangler, architecture

---

## Context

ADR-0034 commits PortfoliFLOW to Postgres as the canonical persistence
backend for the web variant. ADR-0039 commits the migration to the
strangler pattern with a tagged demo-stable branch. Phase 1 implemented
both halves of the substrate: a `PersistentDataStore` subclass of
`DataStore` (Postgres-backed, audit-aware, RLS-protected) was built and
tested but **not** wired into the main code path, and the
`UserRepository` exemplar established the repository-pattern alternative
to the DataStore abstraction.

Phase 2 must now answer: *during the strangler period, how do the two
persistence worlds (in-memory DataStore for the PyQt6 GUI, Postgres for
the FastAPI web variant) coexist*?

Two structurally distinct answers were on the table:

- **(a) Config-flag-driven switch in `get_data_store()`.** A
  `Settings.persistence_backend = "memory" | "postgres"` toggles
  whether `get_data_store()` returns the in-memory singleton or a
  `PersistentDataStore` instance. Both worlds share the same
  application entry point.
- **(b) Separate entry-points by surface.** The PyQt6 GUI continues to
  call `get_data_store()` and receives the in-memory singleton, exactly
  as today. FastAPI routes do **not** call `get_data_store()` — they
  access Postgres exclusively through the repository layer
  (`UserRepository`, future `InvestmentRepository`, etc.). The two
  worlds run in parallel, each with its own persistence surface,
  until Phase 4 retires the in-memory path.

This decision is implementation-relevant rather than security-relevant,
but it has visible consequences for testability, code clarity, and
the strangler trajectory.

## Decision

### 1. Separate entry-points

Phase 2 commits to **(b)**. The PyQt6 GUI keeps its in-memory
`DataStore` singleton; FastAPI routes use the repository layer
directly. The two worlds do not share a configurable persistence
surface.

Concretely:

- `core/data_store.py` and `get_data_store()` continue to mean
  *in-memory singleton, GUI-flavoured API* and nothing else. No
  configuration flag changes their semantics.
- FastAPI route handlers acquire a repository-layer session via the
  established pattern (FastAPI dependency that yields a tenant-scoped
  `AsyncSession` from `tenant_context()`), instantiate the relevant
  repository, and operate exclusively at the DTO boundary.
- The two worlds **do not share data**. An Excel upload via the web UI
  lands in Postgres and is invisible to the GUI; an Excel load via the
  GUI lands in the in-memory store and is invisible to the web UI.
  This is a deliberate strangler-period property, not a defect.

### 2. `PersistentDataStore` is preserved, not retired

The Phase-1 `PersistentDataStore` (Postgres-backed `DataStore`
subclass) is **not** wired into the FastAPI path — it is the wrong
abstraction for repository-flavoured access. Instead, it remains in
the codebase as a tested, Phase-4-ready compatibility layer that
allows the GUI itself to be migrated to Postgres later without
re-engineering its persistence shape.

Phase 4 will evaluate two alternatives for the GUI's persistence
migration:

- Wire `PersistentDataStore` into `get_data_store()` (the
  preserved-Phase-1 path). Smallest GUI-side delta.
- Reshape the GUI to consume the repository layer directly (the
  same path the web variant already uses). Larger delta but
  unifies the two worlds permanently.

The decision between these two is Phase-4 work and is not pre-empted
here. The constraint Phase 2 honours is that **either** path remains
available — `PersistentDataStore` keeps its tests green, the
repository layer keeps growing, and Phase 4 picks based on what it
sees at the time.

### 3. Excel-import write paths during Phase 2/3

A direct consequence of the entry-point separation: Excel imports run
through two distinct write paths during Phase 2 and Phase 3.

- **GUI Excel import** (`modules/front_office/data_import.py`)
  continues to write into the in-memory `DataStore`, unchanged from
  today.
- **Web UI Excel import** (introduced in Phase 2, Sub-Strang 2d) writes
  into Postgres via a new domain-specific repository
  (`InvestmentRepository` and adjacent repositories for NAVs,
  cashflows, attributes — exact factoring is a Sub-Strang-2d
  implementation detail).

The duplication of validation and parsing logic is acknowledged. The
deliberate choice is to defer convergence into a shared service until
Phase 4 — when the GUI itself migrates to Postgres and a single
repository-flavoured persistence path is the natural endpoint. Until
then, dual paths are pragmatic strangler discipline rather than
technical debt: the second consumer (Phase-4 GUI on Postgres) will
make the consolidation obvious, and forcing the convergence
prematurely would couple two refactorings that should remain
sequential.

This Phase-2 dual-path decision is a phase-implementation choice,
not an architecture pattern; it does not get its own ADR. Its
consequences are recorded here.

### 4. Demo discipline during the strangler period

A consequence the operator must hold in mind: a single demo that
displays both GUI and web variants against identical data requires
**manual parallel data loading** during Phase 2 and 3. The
demo-stability checklist explicitly notes this constraint.

Phase 4's persistence convergence resolves the constraint
permanently.

## Rationale

- **API semantic clarity.** A `get_data_store()` function whose return
  type changes meaning under configuration is a function whose
  semantics are configuration-dependent — every caller has to reason
  about the configuration. Tests have to mock or override it.
  Documentation has to caveat it. The cleaner answer is for
  `get_data_store()` to mean exactly one thing.
- **Repository pattern is already established.** `UserRepository`
  exists and demonstrates the pattern. The web path naturally extends
  this pattern; routing the web path through `PersistentDataStore`
  would be force-fitting a GUI abstraction onto a non-GUI surface.
- **Test surface separation.** RLS tests run under the unprivileged
  `portfoliflow_app` role; in-memory `DataStore` tests need none of
  that machinery. Forcing a shared entry-point inflates the GUI test
  setup with database fixtures it does not need.
- **Strangler in its proper shape.** Two worlds living in parallel is
  the canonical strangler structure. A configuration switch turns the
  strangler into a binary toggle, which is a different and weaker
  pattern.
- **Phase 4 optionality preserved.** Keeping `PersistentDataStore`
  tested and ready, while running the web variant on the repository
  layer, allows Phase 4 to choose freely without pre-empting the
  decision.

## Alternatives Considered

- **Config-flag switch in `get_data_store()`.** Rejected for the
  semantic-clarity, test-surface, and strangler-shape reasons above.
- **Retire `PersistentDataStore` outright in Phase 2.** Rejected: it
  is tested, it costs nothing in CI to keep, and Phase 4 may benefit
  from it. Removal without a Phase-4 plan to replace its functionality
  is premature.
- **Force the GUI to use the repository layer in Phase 2.**
  Rejected: this scope-creeps Phase 2 by including Phase-4 work and
  endangers the demo-stable branch. Phase 4 is the right home for
  that migration.

## Consequences

### Positive

- Phase-2 entry points are unambiguous: GUI ↔ in-memory DataStore,
  web ↔ repository layer.
- Repository layer grows organically with web-variant features.
- `PersistentDataStore` retains Phase-4 optionality.
- Tests for the two worlds remain cleanly separable.
- Excel-parsing logic is not refactored under time pressure during
  the largest sub-strang (Phase-2d).

### Negative

- The two worlds do not share data during Phase 2 and 3. Demos that
  compare GUI and web side-by-side require manual parallel data
  loading.
- Excel-import logic is duplicated across the two write paths during
  Phase 2 and 3 (the duplicated portion is the persistence-write
  layer; the parsing layer in `load_excel()` is reused). The
  duplicated surface is small but real.
- Phase 4's persistence convergence is a non-trivial migration. The
  cost is paid once, at a phase boundary, rather than amortised across
  Phase 2 and 3.

### Neutral / Follow-ups

- Phase 4 ADR will record the chosen GUI-on-Postgres approach
  (preserved `PersistentDataStore` vs. repository-layer migration) and
  formally retire `PersistentDataStore` if not selected.
- The Excel-import-service convergence (a `services/excel_import/`
  module with persistence adapters) is held open as a Phase-4
  refactoring opportunity, not a Phase-2 commitment.

## Implementation Notes

- **FastAPI dependency injection.** A `core/repositories/dependencies.py`
  (or `web/dependencies.py`) module defines:
  - `get_engine()` — returns the application-wide `AsyncEngine` (one
    instance, configured at app startup).
  - `get_tenant_session(...)` — FastAPI dependency that yields an
    `AsyncSession` from `tenant_context()` with both `app.tenant_id`
    and `app.user_id` set, sourced from the authenticated session
    (Phase-2b auth middleware).
  - `get_user_repository(session)` etc. — thin factories that wrap a
    session in a repository.
- **No `PersistentDataStore` calls in `web/`.** A regression test
  asserts the absence of `PersistentDataStore` imports anywhere under
  `web/`.
- **Documentation update.** `docs/architecture.md` (and the relevant
  README) gains a section on the dual-persistence-surface arrangement
  during Phase 2 and 3, with the Phase-4 convergence noted as the
  resolution.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:**
  - **Modifiability** — clear separation of persistence surfaces makes
    each surface evolvable without affecting the other.
  - **Testability** — the two persistence surfaces have independent
    test stacks, simplifying verification.
- **No direct security or audit-trail consequence beyond what
  ADR-0034, ADR-0035, and ADR-0036 already commit.** This decision is
  about implementation shape, not access control or accountability.

## References

- ADR-0034 (Persistence Backend: Postgres).
- ADR-0035 (Multi-Tenant Architecture: tenant_id and RLS).
- ADR-0036 (Authentication Strategy).
- ADR-0039 (Migration Pattern: Strangler with Tagged Demo-Stable
  Branch).
- ADR-0040 (Sentinel Bootstrap: CLI-Driven Idempotent Initialization)
  — companion decision shaping Phase-2 deployment.

---

## Revision History

| Date       | Author                       | Change |
|------------|------------------------------|--------|
| 2026-05-04 | PortfoliFLOW project owner   | Initial draft, accepted at Phase-2 kickoff. Separate entry-points: GUI keeps in-memory DataStore, FastAPI routes use repository layer. `PersistentDataStore` preserved as Phase-4-ready compatibility layer but not on the main path. Excel-import dual-write during Phase 2/3 is the consequence; convergence deferred to Phase 4. |
