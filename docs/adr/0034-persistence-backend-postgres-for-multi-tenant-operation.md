# ADR-0034: Persistence Backend — Postgres for Multi-Tenant Operation

- **Status:** Accepted (supersedes ADR-0017)
- **Date:** 2026-05-03
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, persistence, postgres, multi-tenant

---

## Context

PortfoliFLOW persists data today through `DataStore`
(`core/data_store.py`), an in-memory dict of named pandas DataFrames
fronted by `get_data_store()`. The module's docstring explicitly
anticipates a `PersistentDataStore` subclass that overrides the
storage methods against a real backend — the public API
(`store`, `get`, `list`, `remove`) was deliberately shaped so
modules do not have to change when persistence is introduced.

ADR-0017 selected **DuckDB** as that future backend. The decision
rested on a single-user desktop premise: an embedded, columnar,
file-based database aligned with one operator running analytical
queries against their own data, with a single sentinel `created_by`
identity for the audit fields specified by ADR-0019. Under that
premise the choice was correct.

ADR-0033 invalidates the premise. The web migration commits
PortfoliFLOW to a multi-tenant deployment where several users from
the same organisation share a workspace and where multiple
organisations may share a single deployment. Four properties become
non-negotiable that DuckDB does not deliver:

1. **Multi-writer concurrency.** Several users in the same tenant
   write at the same time. DuckDB is an analytical engine optimised
   for single-writer workloads; multi-writer concurrency is outside
   its operating envelope.
2. **Row-level security.** Tenant isolation cannot rely on a
   `WHERE tenant_id = ?` clause that the application is responsible
   for adding. A single missing predicate would leak data across
   organisational boundaries — a class of bug that institutional
   audit cannot tolerate. DuckDB has no native RLS facility.
3. **Trigger-based audit.** Every write must be recorded in an audit
   log without relying on application code remembering to log it.
   DuckDB's trigger story is thin compared to Postgres's mature
   PL/pgSQL infrastructure.
4. **Operational maturity.** Backup, point-in-time recovery, logical
   replication, connection pooling, and monitoring are expected by
   the institutional ops teams that PortfoliFLOW will be deployed
   alongside. Postgres has decades of those tools shipped, packaged,
   and documented.

DuckDB's strengths (embedded deployment, columnar query speed,
Parquet interoperability) remain real, but they no longer dominate
the requirements list. The right answer is a managed relational
database with first-class RLS, mature concurrency, and an audit-
friendly trigger model. Postgres is that database.

This decision touches several institutional regulatory domains:
BAIT/VAIT (IT operations and audit-trail expectations), MaRisk AT
7.2 (IT-risk management and tenant separation), DSGVO Art. 32
(security of processing), GoBD (immutability and traceability of
business records), and ISO 25010 (Reliability, Security,
Maintainability, Portability). The persistence layer is the locus
where most of those expectations are technically anchored.

## Decision

PortfoliFLOW uses **PostgreSQL** as the sole persistence backend in
the web variant. This ADR supersedes ADR-0017.

The decision has the following components:

### 1. Postgres version

Minimum **15**, target **16+**. The lower bound is set by RLS
maturity (especially around `BYPASSRLS`, `FORCE ROW LEVEL SECURITY`,
and policy expressions) and logical replication features used in
deployment scenarios. Implementation work pins the exact version in
`pyproject.toml`-adjacent deployment manifests; this ADR does not
freeze a single version because the deployment topology is still
open per ADR-0033.

### 2. Schema principles — binding for every new table

Every new domain table satisfies the following invariants. Detail
of how `tenant_id` and RLS are wired together is in ADR-0035; this
ADR makes them mandatory at the schema level.

- **`tenant_id UUID NOT NULL`**, foreign key to `tenants(id)`, on
  every table that holds domain data (investments, SAA models,
  reports, documents, charts, asset classes, currencies, sectors,
  etc.). This column is the anchor of the tenant-isolation policy
  recorded in ADR-0035.
- **`owner_id UUID NOT NULL`**, foreign key to `users(id)`, on
  every table whose records have a primary responsible user
  (typically user-created content: investments, SAA models,
  reports). Lookup tables without a meaningful owner may omit this
  column; the omission must be documented at the table definition.
- **Audit fields (per ADR-0019):**
  - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
  - `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
  - `created_by UUID` (FK to `users(id)`, nullable for
    system-generated rows)
  - `updated_by UUID` (FK to `users(id)`, nullable)
- **`source TEXT NOT NULL`** — provenance of the row. Initial
  dominant value is `'excel-import'`; other values are introduced
  as connectors land (`'manual-entry'`,
  `'api-connector:bloomberg'`, `'system-generated'`, ...). The
  column is the structural extension point for non-Excel data
  sources without a schema migration.
- **`version INTEGER NOT NULL DEFAULT 1`** for optimistic locking
  on tables whose records are mutable in user workflows
  (Investments, SAA models). Append-only or audit-only tables omit
  the column.

A concrete example of the column block (illustrative only — actual
DDL lives in Alembic migrations):

```sql
tenant_id    UUID        NOT NULL REFERENCES tenants(id),
owner_id     UUID        NOT NULL REFERENCES users(id),
created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
created_by   UUID        REFERENCES users(id),
updated_by   UUID        REFERENCES users(id),
source       TEXT        NOT NULL,
version      INTEGER     NOT NULL DEFAULT 1
```

### 3. Repository pattern as the access layer

Persistence access is exclusively through repository classes located
under `core/repositories/`. Services call repositories;
repositories call the database. Direct SQL or ORM calls from
services, modules, GUI widgets, or web routes are forbidden. This
gives ADR-0018's planned Service / Repository layering its
technical form for the web variant.

Repository methods accept and return plain Python dataclasses /
Pydantic models — never SQLAlchemy session-scoped instances —
so consumers do not depend on the ORM lifecycle.

### 4. Migration path from `DataStore`

The `PersistentDataStore` subclass anticipated by `core/data_store.py`
is implemented as a Postgres-backed store in **Phase 1** (per
ADR-0033's phased plan):

- **Phase 1.** `PersistentDataStore` introduced. The factory
  `get_data_store()` selects the backend based on a config flag
  (the docstring already names this approach). Both backends remain
  available; the in-memory variant is the default until Phase 2.
- **Phase 2.** `PersistentDataStore` becomes the default.
  In-memory variant remains for tests and developer workflows.
- **Phase 4 / 5.** In-memory variant is reduced to test fixtures
  only; no production code path uses it.

### 5. Schema migration tooling: Alembic

All schema changes are Alembic migrations under `db/migrations/`,
with both `upgrade()` and `downgrade()` paths. Migrations are
checked into the repository, reviewed alongside the code that
depends on them, and applied via `alembic upgrade head` during
deployment.

Forward-only migrations are forbidden in Phase 1–4: rollback must be
possible until the deprecation of the desktop variant. After that,
forward-only migrations are admissible for cleanups that are
genuinely irreversible (column drops after a multi-release
deprecation), and only with an ADR-level note.

### 6. Connection handling

The application uses an async Postgres driver. Two options are
admissible for the implementation choice in Phase 1:

- **`asyncpg`** directly — minimal layer, full control.
- **`SQLAlchemy 2.x` with async support over `asyncpg`** — the ORM
  fits naturally with the repository pattern and with Alembic.

The recommendation is SQLAlchemy 2.x async, because it makes the
repository layer cheaper to implement and migrations cheaper to
generate. Implementation work commits to one of the two and removes
the alternative from this ADR's open list at that point.

Connection pooling is at the application level. External pooling
(PgBouncer, Pgpool) is a deployment-time decision, deferred to the
deployment topology that ADR-0033 leaves open.

### 7. Data sources

Phase 2 admits **Excel upload as the only entry point** for domain
data. The user uploads an Excel file, the server parses it, and the
parsed rows are written to Postgres with `source = 'excel-import'`.

The schema is open for future ingestion paths via the `source`
column: connectors to administration systems, Bloomberg / Refinitiv
feeds, custom APIs, or scraped GP reports can write rows with their
own `source` values without a schema migration. This ADR does not
commit to the connector model; it commits to the schema being able
to host one.

### 8. Backup and disaster recovery

Backup strategy and recovery objectives are **deployment-specific**
and not architecturally fixed here. A SaaS instance needs hard
RPO/RTO targets with audit evidence; an on-premise installation
follows the customer IT's backup procedures. ADR-0033 places the
final backup/DR decision in Phase 5.

This ADR commits the architecture to providing **clean backup
hooks**:

- Standard Postgres dumps (`pg_dump`, `pg_basebackup`) work without
  application-specific configuration. No data is hidden in
  application memory after a transaction commits.
- File assets that live outside Postgres (uploaded documents,
  generated PDFs, log files) are kept in clearly delimited
  directories so they can be backed up alongside the database with
  a standard filesystem snapshot.
- Secrets are externalised (environment, secret manager) — never
  stored inside the database where they would travel with backups.

## Rationale

- **Postgres against other relational alternatives.** RLS in
  Postgres is markedly more mature than in MariaDB/MySQL, where the
  same effect is reachable only through views or triggers. Oracle
  and SQL Server fall outside the open-source / cost envelope that
  the institutional on-premise customer base typically expects.
  Postgres is the de-facto choice for DSGVO-conscious European
  deployments, and operating it in Germany is unremarkable.
- **Single database vs. Postgres + DuckDB hybrid.** A second
  analytical database in the stack would cost operational effort
  (deployment, monitoring, version coordination) and synchronisation
  complexity. The expected data volumes for the medium term fit
  comfortably inside Postgres with appropriate indexing and, where
  needed, materialised views. If analytical pressure grows past
  what Postgres can handle gracefully, DuckDB-as-read-replica is
  available as a future extension under its own ADR.
- **Strict schema discipline from day one.** Multi-tenant data
  modelling is not retrofittable safely. A table without
  `tenant_id` introduced under deadline pressure cannot be made
  multi-tenant later without a migration that touches every row,
  every query, and every test. Naming the discipline now removes
  that risk.
- **Repository pattern over direct ORM access.** Repositories make
  the persistence layer testable without a real database (via
  in-memory fakes or PostgreSQL test containers), keep the domain
  layer ignorant of SQLAlchemy lifecycle concerns, and give the
  layered architecture established in ADR-0001 a place to sit.
  ADR-0018 already names this layering as the prerequisite for
  client-server topology; this ADR is where it gets technical
  form.
- **Alembic as the migration tool.** Alembic is the standard for
  SQLAlchemy projects, supports branch-aware migrations, and
  produces deterministic SQL. No custom migration tooling.
- **Phased migration path through `PersistentDataStore`.** The
  existing `DataStore` API was deliberately shaped (in ADR-0004's
  spirit) so that persistence could be added under the same
  surface. Honouring that contract in Phase 1 means the rest of the
  application does not change while the backend is swapped.

## Alternatives Considered

- **Keep DuckDB as planned in ADR-0017.** Rejected. DuckDB does not
  deliver multi-writer concurrency, RLS, or trigger-based audit at
  the level multi-tenant operation requires. It would also rule out
  any future SaaS deployment.
- **SQLite plus an application-level RLS layer.** Rejected. SQLite
  has no real multi-writer story (database-level write lock).
  Application-level tenant filtering is exactly the failure mode
  RLS is meant to prevent: a single missing `WHERE tenant_id = ?`
  leaks data, and the failure is silent.
- **MariaDB / MySQL.** Considered. Functionally close, but RLS is
  strictly weaker and the customer base for institutional
  finance-adjacent software tilts toward Postgres. The argument
  against MariaDB/MySQL is moderate, not crushing — but Postgres
  wins on RLS maturity, JSONB ergonomics, and ecosystem fit.
- **Document database (MongoDB, CouchDB).** Rejected. The domain
  model has rich relational structure (investments link to SAA
  models, returns link to currencies, transactions link to
  investments). Forcing it into a document model would introduce
  duplication and complicate referential integrity, which audit
  expectations rely on.
- **Postgres plus DuckDB as analytical companion from day one.**
  Rejected for the current phase. Two databases in the stack add
  operational and synchronisation overhead that the expected
  workloads do not yet justify. Held open as a future extension if
  analytical pressure grows.
- **Cloud-managed proprietary DB (e.g. Cloud Spanner, Aurora).**
  Rejected. Locks in a specific cloud and contradicts the
  on-premise / EU-cloud / SaaS optionality that ADR-0033
  preserves.

## Consequences

### Positive

- Multi-tenant data modelling is structurally available from day
  one. No retrofit migration looms.
- RLS, audit triggers, and operational tooling are native rather
  than reconstructed.
- Backup, recovery, replication, and monitoring leverage standard
  Postgres tooling — no custom paths to maintain.
- Audit-relevant properties (provenance via `source`, accountability
  via `created_by` / `updated_by`, integrity via referential
  constraints) are encoded directly in the schema, where they are
  least likely to drift from reality.
- `DataStore`'s subclass-based extension point matures into its
  intended role; modules do not change.

### Negative

- Postgres is an additional process in the stack relative to a
  pure file-based database. On-premise deployments add a
  database-administration footprint, even if Postgres is typically
  already present in the target environments.
- Schema migrations require discipline: every Phase 1+ change must
  ship with `upgrade()` and `downgrade()`, and migrations must be
  tested against representative data before deployment.
- Connection management gets more complex: every connection must
  set the tenant context (per ADR-0035) and unset it on release.
  The convention is enforceable in code but is application-level
  discipline, not a database-level guarantee.
- DuckDB-specific optimisations (columnar storage, vectorised
  execution, native Parquet I/O) are not available; analytical
  workloads must use Postgres-native facilities (BRIN indexes,
  materialised views, well-chosen partitioning) when needed.
- A new dependency surface (Alembic, asyncpg or SQLAlchemy 2.x
  async, Postgres itself) joins the stack. CVE surveillance and
  upgrade discipline grow accordingly.

### Neutral / Follow-ups

- Postgres operational experience exists in the project context;
  no new learning curve at the framework level.
- The in-memory `DataStore` is preserved for tests and ad-hoc
  developer workflows; not deleted.
- Connector model for non-Excel data sources is structurally
  prepared via `source` but not committed in this ADR. A separate
  ADR will follow when the first non-Excel connector lands.
- Backup / DR strategy is deployment-specific and lands in Phase 5.

## Implementation Notes

- **Driver and ORM choice:** finalise in Phase 1 (recommendation:
  SQLAlchemy 2.x async over asyncpg).
- **Migration tool:** Alembic. Migrations live under
  `db/migrations/`. Configuration in `db/alembic.ini` or an
  equivalent location; settled at implementation time.
- **Repository layer:** classes under `core/repositories/`. ORM
  models (if SQLAlchemy is selected) under `core/models/`. The
  existing `core/` rule that core imports nothing from the rest of
  the project remains in force; SQLAlchemy and asyncpg are
  third-party packages, which is permitted.
- **Sentinel-tenant bootstrap:** an Alembic seed migration (or a
  dedicated CLI command) creates the sentinel tenant and the
  sentinel user from environment variables (`SENTINEL_EMAIL`,
  `SENTINEL_PASSWORD`). Idempotent. Detail in ADR-0036.
- **Test database:** Postgres test containers (e.g., via
  `testcontainers-python`) for integration tests. Repository tests
  must run against a real Postgres instance with RLS active —
  superuser shortcuts in tests would mask exactly the bugs RLS is
  there to catch (per ADR-0035).
- **Connection-string handling:** read from `DATABASE_URL` in
  `.env`. Production deployments override via the environment.
  Secrets live outside the codebase.
- **Backup hooks:** standard Postgres tooling without
  application-specific knowledge. File-asset directories are kept
  separate and documented at deployment time.
- **Migration of `DataStore`:** the in-memory variant remains as
  the default in Phase 1; the persistent variant becomes default
  in Phase 2. Selected via `Settings.persistence_backend` (or the
  config flag named in the existing `DataStore` docstring).

## Compliance & Audit Relevance

- **BAIT (AT 7.2 — IT operations) and VAIT (Chapter 5 — IT
  operations).** Centralised persistence with audit triggers is the
  kind of structural control these frameworks expect to see for
  systems that hold regulated data. Audit columns
  (`created_by`, `updated_by`, `created_at`, `updated_at`,
  `source`) supply the provenance that audit reviews look for.
  Tenant isolation lands in ADR-0035; this ADR delivers the
  schema substrate.
- **MaRisk AT 7.2 — separation of test and production data.**
  Achieved at deployment level by separate Postgres instances per
  environment; the repository pattern makes test fixtures
  inexpensive. No production-data leakage into test environments.
- **DSGVO Art. 32 (security of processing).** Postgres supports
  encryption at rest (filesystem-level or TDE solutions, depending
  on deployment) and TLS in transit. Specific configuration is a
  deployment-time decision and is captured in deployment
  documentation, not here.
- **GoBD (Grundsätze ordnungsmäßiger Buchführung).** Append-only
  audit logs (per ADR-0035), immutable timestamps, and the
  schema-level audit columns provide the traceability and
  unalterability properties that GoBD-relevant business records
  expect. Concrete domain-by-domain treatment (e.g. ledger-style
  tables vs. mutation with audit log) is a per-domain decision in
  later implementation work.
- **ISO 25010 quality attributes affected:**
  - **Reliability** — mature operations stack, PITR, replication.
  - **Security** — RLS substrate, encryption support, TLS.
  - **Maintainability** — repository pattern, Alembic-managed schema
    evolution.
  - **Portability** — standard SQL, OS-portable Postgres builds,
    standard backup formats.
- **DORA (Operational Resilience).** Postgres is a proven backbone
  for the operational-resilience requirements DORA names (backup,
  recovery, change management). Concrete measures land in Phase 5
  alongside the deployment-topology decision.
- **Audit evidence:** Alembic migration history; `pg_policies`
  contents at any point in time (per ADR-0035); `audit_log` table
  contents (per ADR-0035); repository-layer tests with RLS active.

## References

- ADR-0001 (Layered Architecture) — repositories sit in `core/`,
  consistent with the layering rules.
- ADR-0004 (In-Memory DataStore Singleton) — the API that
  `PersistentDataStore` continues; not changed.
- ADR-0017 (Planned DataVault — DuckDB) — **superseded by this
  ADR**.
- ADR-0018 (Planned Service / Repository Layering) — gets
  technical form here.
- ADR-0019 (Planned Multi-User Readiness) — audit fields named
  there are made binding here.
- ADR-0033 (Web Migration: Architectural Shift) — the strategic
  frame that motivates this decision.
- ADR-0035 (Multi-Tenant Architecture) — applies these schema
  principles to enforce tenant isolation.
- ADR-0036 (Authentication Strategy) — the user table that the
  audit columns reference lives in this persistence backend.
- `core/data_store.py` — the existing API whose `PersistentDataStore`
  extension point is consumed here.

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-05-03 | PortfoliFLOW project owner   | Initial draft. Selects PostgreSQL as the persistence backend for the web variant, supersedes ADR-0017's DuckDB choice, and binds the schema-level invariants (`tenant_id`, `owner_id`, audit fields, `source`) that ADR-0035 then enforces via RLS. |
| 2026-05-03 | PortfoliFLOW project owner   | Status moved to **Accepted**. Phase 1, Strang B landed: `compose.yml` plus `db/postgresql.conf` and `db/init/01-create-app-role.sql` provide a reproducible local Postgres 16; the initial Alembic migration creates `tenants`, `users`, `audit_log` (and `data_store_entries` in a follow-up migration) with `ENABLE` + `FORCE ROW LEVEL SECURITY` on every domain table per ADR-0035. The repository layer's first concrete consumer (`UserRepository`) and `PersistentDataStore` are implemented and tested against the live compose DB; the persistent variant is **not** wired into `get_data_store()` — the in-memory `DataStore` remains operational. SQLAlchemy 2.x async over asyncpg is the chosen driver/ORM stack (per Implementation Notes); Alembic 1.18 with async `env.py` reads `DATABASE_URL_SUPERUSER` from `.env` and runs as the Postgres superuser. Sentinel-tenant / sentinel-user bootstrap deferred to Phase 2. Decider: PortfoliFLOW project owner. |
