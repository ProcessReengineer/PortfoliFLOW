# ADR-0019: Planned Multi-User Readiness via Audit Fields, No Multi-User Code Yet

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** architecture, security

---

## Context

PortfoliFLOW is currently a single-user desktop application. The realistic target audience for a future version is a small team (3–8 users — operations, investment, reporting). Building a full multi-user system now would be premature: authentication, authorisation, session management, concurrency control, and conflict resolution are each non-trivial and would consume effort better spent on core features.

At the same time, two design choices being made now will be very expensive to retrofit if multi-user happens later:

1. The DataVault schema (ADR-0017) — adding `created_by` / `modified_by` after the fact requires backfilling every row.
2. Embedding business logic in PyQt6 classes (ADR-0018) — a multi-user backend cannot reuse logic that requires `QApplication`.

The decision PortfoliFLOW must make now is therefore *what to invest in today so that multi-user remains a feasible future*, not whether to ship multi-user now.

## Decision

PortfoliFLOW will not build any multi-user code (authentication, authorisation, session, sync) at this time. However, two structural decisions are made now to preserve the option:

1. The DataVault (ADR-0017) includes `created_by`, `created_at`, `modified_by`, `modified_at`, and `source` columns on every table from day one. In single-user mode, `created_by` / `modified_by` are populated with a single sentinel user identity (e.g., the OS user); when multi-user is introduced, no schema change is required.
2. Service / Repository layering (ADR-0018) is pursued so that business logic does not assume single-user / single-process semantics.

Authentication strategy, authorisation model, secrets management, session handling, and conflict resolution are deliberately **not decided here** and are flagged for separate ADRs when multi-user becomes a real near-term target.

## Rationale

- Audit fields cost almost nothing today and dramatically reduce the cost of multi-user later.
- Service / Repository layering is justified independently (ADR-0018); the multi-user case reinforces it.
- Building authentication / authorisation now would be premature optimisation against an unconfirmed timeline and would block more important work.
- Explicitly listing the *non-decisions* (auth, secrets, session, conflict) in this ADR documents that they are open and prevents informal assumptions from sneaking in.

## Alternatives Considered

- **Build a multi-user system now:** Rejected — premature; the user base is one person today, and multi-user introduces large surfaces (auth, RBAC, concurrency) for no current benefit.
- **Defer audit fields until multi-user happens:** Rejected — backfilling provenance across an institutional dataset is exactly the kind of regulatory smell to avoid.
- **Defer the layering decision (ADR-0018) until multi-user happens:** Rejected — same retrofit cost argument.
- **Adopt a multi-user architecture pattern (CRDT, event sourcing) speculatively:** Rejected — over-engineering for a team that may never grow past a handful of users.

## Consequences

### Positive

- The DataVault (ADR-0017) is born multi-user-ready in its schema.
- Business logic, as it migrates into the Service Layer (ADR-0018), is naturally reusable by a backend later.
- The cost of saying "we want multi-user now" is bounded and known.

### Negative

- Audit columns add a small storage / write overhead for the single-user case.
- A sentinel `created_by` for the single user is an awkward shape that future code must accept.
- Open questions (auth, secrets, session, conflict) accumulate as planning debt; if multi-user is suddenly required, those decisions all have to be made at once.

### Neutral / Follow-ups

- Once multi-user becomes a real near-term target, write separate ADRs covering at minimum: authentication strategy, authorisation / RBAC model, secrets management, session handling, concurrency / conflict resolution.
- Decide on the sentinel value used for `created_by` in single-user mode (e.g., `"local"`, the OS username, or a configured identifier).
- Tool access control in `services/tool_registry.py` (ADR-0012) is currently absent; revisit when the multi-user case appears.

## Implementation Notes

- Affects: DataVault schema (per ADR-0017) and the Service / Repository layering (per ADR-0018).
- Documented in: `CLAUDE.md` ("Multi-User").
- No code change required by this ADR alone; it constrains the design of ADR-0017 and ADR-0018.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Security (deliberately deferred but constrained), Maintainability (audit fields enable provenance), Compatibility (single-user / multi-user transition).
- **Regulatory references:** Audit-trail expectations in BAIT/VAIT and DORA-style change records (the audit fields are the concrete preparation for these).
- **Audit evidence:** ADR-0017's schema definition, this ADR documenting the deliberate non-decisions.

## References

- ADR-0012 (ToolRegistry — currently has no per-user access control)
- ADR-0017 (Planned DataVault — provides the audit fields)
- ADR-0018 (Planned Service / Repository layering — the structural prerequisite)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from "Planned Architecture" notes in `CLAUDE.md`; no implementation yet. |
| 2026-05-03 | PortfoliFLOW project owner            | Phase 1, Strang B: the audit-trigger pattern named here is now structurally active. `tenant_id NOT NULL` is enforced on every Phase-1 domain table (`users`, `audit_log`, `data_store_entries`); the `users` table fires `audit_trigger_function()` on every INSERT/UPDATE/DELETE and writes a JSONB before/after pair to `audit_log`. The trigger reads `app.user_id` via `current_setting(..., true)`, which produces NULL today (no auth wired); Phase 2's auth middleware will populate the GUC alongside `app.tenant_id` without a schema migration. The sentinel-user bootstrap is deferred to Phase 2 (per ADR-0036 §6); Phase 1's operational path remains the in-memory `DataStore`, which has no audit fields by design. The structural readiness this ADR called for is now in place. Decider: PortfoliFLOW project owner. |
| 2026-05-20 | PortfoliFLOW project owner            | Promoted to Accepted. The audit substrate, multi-tenant isolation (ADR-0035), and authentication surface (ADR-0036) are all operational; the conditions originally noted ("audit substrate active in Phase 1, Strang B") are satisfied. Closes P6-E. |
