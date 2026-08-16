# ADR-0017: Planned DataVault — DuckDB-Backed Persistent Layer with Audit Fields

- **Status:** Superseded by ADR-0034 (2026-05-03)
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** data, architecture

---

## Context

> **Note:** This ADR has been superseded by ADR-0034 (Persistence Backend:
> Postgres for Multi-Tenant Operation), which replaces the planned DuckDB
> backend with Postgres to support the Web Migration's multi-tenant
> requirements (see ADR-0033). The original content of this ADR is preserved
> below for historical reference.

The current DataStore (ADR-0004) is in-memory only. Data does not survive an application restart; users must re-import every session. As PortfoliFLOW takes on real institutional workloads — quarterly fund reports ingested by the planned Report Scraper, multi-quarter time series, scraped GP data — the absence of persistence becomes a hard limitation.

A persistent layer must answer several questions deliberately rather than by default: which storage technology, where the file lives, what schema-level constraints apply, and what audit fields are recorded so the layer is fit for institutional / compliance review.

This ADR captures the decisions already taken (in `CLAUDE.md` under "Planned Architecture — DataVault"), with status `Proposed` because no code has been written yet.

## Decision

PortfoliFLOW will introduce a new persistent data layer called the **DataVault**, distinct from the in-memory DataStore (ADR-0004). The DataVault will use **DuckDB** as its embedded, file-based, columnar storage engine. The current intended file location is `~/.portfoliflow/datavault.duckdb` (to be confirmed at implementation time).

Every table in the DataVault will include the following audit fields from day one:

- `created_by` (str)
- `created_at` (datetime)
- `modified_by` (str)
- `modified_at` (datetime)
- `source` (str — e.g., `"manual"`, `"scraper"`, `"import"`)

The DataVault is the authoritative, auditable record of all fund and portfolio data that PortfoliFLOW ingests or computes. The DataStore (ADR-0004) remains the in-process working copy.

## Rationale

- DuckDB is embedded (no separate server), file-based (one file per environment, easy to back up and reason about), and columnar (well-suited to the analytical, time-series queries PortfoliFLOW runs). It also reads Parquet directly, which keeps the door open for hybrid storage later.
- Audit fields applied uniformly from the first table mean compliance / audit reviews can rely on every record having provenance — adding them later is dramatically more expensive.
- Separating DataVault (persistent, authoritative) from DataStore (in-memory, working copy) preserves the option to keep the working-copy semantics simple (e.g., copy-on-read) while persistence concerns live behind a Repository layer (ADR-0018).

## Alternatives Considered

- **SQLite:** Rejected — row-oriented, weaker for analytical queries over wide time-series tables, no native columnar/Parquet integration.
- **PostgreSQL:** Rejected for current scale — adds a server dependency the single-user desktop deployment does not need; revisit if multi-user (ADR-0019) becomes a real near-term target.
- **Plain Parquet files on disk (no engine):** Rejected — would require re-implementing query, schema, and constraint handling.
- **Continue with in-memory DataStore only:** Rejected — does not meet the persistence and audit requirements that institutional workloads bring.
- **Add audit fields later:** Rejected — retrofit cost and the gap in audit history are both unacceptable; add them from day one.

## Consequences

### Positive

- Data survives restarts and is queryable historically.
- Every record has provenance (created_by, created_at, source) suitable for institutional audit.
- Analytical queries (cross-quarter, cross-fund) become first-class.
- Backup is a single file copy.

### Negative

- A new technology (DuckDB) enters the dependency surface; the team must learn it and version it carefully.
- Audit fields impose discipline on every write path: who is `created_by` when an automated scraper writes? Decisions about identity / "system" users have to be made before the first write.
- File-based storage limits concurrent writers; a future multi-user (ADR-0019) deployment will need to revisit this.

### Neutral / Follow-ups

- A Repository layer (ADR-0018) must exist before the DataVault is wired to the rest of the application.
- A migration strategy from the current in-memory model to the DataVault must be designed (initially: import paths write to both DataStore and DataVault; consumers continue reading DataStore).
- Backup, retention, and deletion policies are all open questions — flagged in the retrofit gaps.
- Confirm the file location (`~/.portfoliflow/datavault.duckdb` is currently a working assumption).

## Implementation Notes

- Not yet implemented. Implementation should be preceded by an architecture-review pass per ADR-0015.
- Documented in: `CLAUDE.md` ("Planned Architecture — DataVault").
- Will affect: a new `core/datavault.py` (or its own top-level `datavault/` package — to be decided), Repository classes per domain (ADR-0018), and the import path in `modules/front_office/data_import.py`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Reliability (data durability), Security (audit trail), Maintainability (schema evolution), Compatibility (Parquet interop).
- **Regulatory references:** General audit-trail expectations (BAIT/VAIT and DORA-style change/operational records) are part of the motivation for the audit fields.
- **Audit evidence (once implemented):** Schema definitions including the five audit columns; database file location and access controls; backup policy.

## References

- ADR-0001 (Layered architecture)
- ADR-0004 (In-memory DataStore — the working-copy companion to DataVault)
- ADR-0018 (Planned Service / Repository layering — must precede DataVault wiring)
- ADR-0019 (Planned multi-user readiness — drives some of the audit-field design)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from "Planned Architecture" notes in `CLAUDE.md`; no implementation yet. |
| 2026-04-29 | PortfoliFLOW project owner            | Cross-reference note: the Qt-free `services/headless_shirley.py` (ADR-0029) is the architectural precondition this ADR's eventual implementation will sit behind, and ADR-0031 names the concurrency follow-up that becomes resolvable once the DataVault replaces the in-memory DataStore. Decision body unchanged. |
| 2026-05-03 | PortfoliFLOW project owner            | Marked as superseded by ADR-0034. The DuckDB backend selected here is replaced by Postgres to satisfy the multi-tenant requirements of the Web Migration (ADR-0033). Original decision body preserved unchanged for historical reference. |
