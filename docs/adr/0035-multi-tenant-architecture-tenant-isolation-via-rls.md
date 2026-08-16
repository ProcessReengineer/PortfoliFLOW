# ADR-0035: Multi-Tenant Architecture — Tenant Isolation via tenant_id and Row-Level Security

- **Status:** Accepted
- **Date:** 2026-05-03
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, multi-tenant, security, data-isolation

---

## Context

ADR-0033 commits PortfoliFLOW to a multi-tenant web architecture.
ADR-0034 selects Postgres as the persistence backend and binds the
schema-level invariants (`tenant_id NOT NULL` on every domain table,
audit columns, `source` provenance). What both ADRs deliberately
postpone is *how tenant isolation is technically enforced* — the
mechanism that turns the `tenant_id` column from a labelling
convention into a security boundary.

A **Tenant** in PortfoliFLOW is the organisational boundary of data
isolation: a FoF boutique, a Versorgungswerk, an asset manager.
Multiple users inside one tenant share access to investments, SAA
models, and reports under a fine-grained permission overlay. **No
data is ever shared across tenants.** Two tenants on the same
deployment are as isolated, from each other's perspective, as if
they ran on physically separate systems.

Why isolation cannot be left to application-level filtering:

- **Application code has bugs.** A repository method that forgets
  to add `WHERE tenant_id = ?` to a query returns data from every
  tenant. The bug is silent: the query succeeds, the response shape
  is correct, the only symptom is data appearing in places it must
  not. Audit cannot accept that failure mode.
- **Joins multiply the failure surface.** Even when the primary
  table is filtered correctly, joined lookup tables (asset classes,
  currencies, sectors) without a `tenant_id` predicate would leak
  cross-tenant data through a perfectly innocent-looking JOIN.
- **The institutional audience expects defence in depth.** A
  Versorgungswerk asking "how do you guarantee my data is not
  visible to another customer" wants a structural answer, not a
  promise that the application is well-tested.

Postgres ships exactly the right primitive: **Row-Level Security
(RLS)** with policies that the database evaluates on every query,
independently of how the application is written. A correctly
configured RLS policy turns a missing `WHERE tenant_id` from a
silent leak into either a denied access or an empty result. The
policy is auditable (`pg_policies`), reviewable (in code, via
Alembic migrations), and enforceable in tests.

This ADR binds that primitive to the project. It does not invent
new policy semantics — it commits PortfoliFLOW to using the
established Postgres feature consistently and strictly.

This decision is security-, audit-, and compliance-relevant. BAIT
(AT 7.2, AT 7.3), VAIT (Chapter 5, 6), MaRisk AT 7.2, DSGVO
Art. 25 / Art. 32, and ISO 25010 (Confidentiality, Maintainability,
Reliability) all bear on it.

## Decision

PortfoliFLOW enforces tenant isolation through Postgres Row-Level
Security on every domain table, with the application setting the
tenant context per database connection. The decision has the
following components.

### 1. `tenant_id UUID NOT NULL` on every domain table

`tenant_id` is `NOT NULL` on every table that holds domain data,
references `tenants(id)`, and uses `ON DELETE RESTRICT`. Tenants
cannot be hard-deleted while data still exists; deletion is a
deliberate workflow with explicit data handling, not a cascading
side effect. (The deletion workflow itself is a Phase-5 concern.)

### 2. RLS policy on every domain table

Every domain table has Row-Level Security enabled and forced (so
even table owners are subject to it):

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <table> FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON <table>
  USING       (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK  (tenant_id = current_setting('app.tenant_id')::uuid);
```

The `USING` clause filters reads and the visibility of rows for
update / delete; the `WITH CHECK` clause prevents the application
from inserting or updating a row into a different tenant. The
policy applies uniformly to `SELECT`, `INSERT`, `UPDATE`, and
`DELETE`.

A reusable PL/pgSQL helper (e.g. `apply_tenant_rls(table_name)`)
attached to the schema generates this block from the table name,
ensuring that every Alembic migration applies the standard policy
without copy-pasting SQL. Variants are forbidden without an ADR.

### 3. Strict variant — no exceptions for lookup tables

Lookup tables (asset classes, currencies, sectors, country codes,
benchmark indices) also carry `tenant_id` and have an RLS policy.
At tenant onboarding, the system seeds these tables with the
default content for that tenant; the seeded rows are owned by the
tenant and overrideable by it.

The strict variant is chosen deliberately over a "global lookup
tables" alternative. A global, RLS-exempt table is a special case
that every query joining it must remember to handle correctly. One
forgotten special case is one cross-tenant leak. Strict uniformity
removes the special case entirely: all tables behave the same way.
The cost is duplicate seed data; the benefit is no exception
surface.

### 4. Tenant-context setup per database connection

Every application database connection sets the tenant context
**immediately after acquiring the connection from the pool, inside
the transaction** that the work will run on:

```sql
SET LOCAL app.tenant_id = '<tenant-uuid>';
```

The `LOCAL` keyword is essential: without it, the setting persists
on the connection after release and would leak to the next request
that picks up the same pooled connection. With `LOCAL`, the setting
ends with the transaction, which is exactly the granularity the
isolation model needs.

A repository-layer context manager (or equivalent dependency-
injected wrapper, settled at implementation time per ADR-0034)
performs the `SET LOCAL` and is the only sanctioned way to obtain
a tenant-scoped database session. Direct session acquisition that
bypasses the wrapper is a programming error.

### 5. Sharing within a tenant

Sharing of resources between users belonging to the same tenant
is expressed via a `resource_permissions` table. The conceptual
shape is (precise schema lands in implementation, including
indexing decisions):

```
resource_permissions
  id               UUID PRIMARY KEY
  tenant_id        UUID NOT NULL REFERENCES tenants(id)
  resource_type    TEXT NOT NULL          -- 'investment', 'saa-model', 'report', ...
  resource_id      UUID NOT NULL
  user_id          UUID NOT NULL REFERENCES users(id)
  permission_level TEXT NOT NULL          -- 'read', 'write', 'admin', ...
  + standard audit fields (per ADR-0034)
  UNIQUE (tenant_id, resource_type, resource_id, user_id)
```

The minimum permission levels are `'read'` and `'write'`. Further
levels can be added without a schema change. The default visibility
of a resource within its tenant (only the owner, only sharers,
or everyone in the tenant) is a Phase-5 product decision; this ADR
commits the data model.

`resource_permissions` itself carries `tenant_id` and an RLS
policy.

### 6. Cross-tenant sharing is forbidden

There is no resource that belongs to multiple tenants. There is no
"system" or "global" tenant accessible to others. Two policies
enforce the rule:

- **Database level.** RLS prevents reads, inserts, updates, and
  deletes that target a different `tenant_id` than the connection's
  current setting.
- **Application level.** The repository layer never accepts a
  tenant ID different from the one set on the active connection.
  Cross-tenant intent in code is a fail-fast error.

Both layers must be wrong for a leak to happen — the defence-in-
depth property the institutional audience expects.

If, in the future, a use case appears that genuinely demands
cross-tenant data flow (e.g., an industry-wide benchmark service),
it is solved as an integration *between deployments*, not as an
exception to this rule.

### 7. Audit logging via triggers

Every write to a domain table (`INSERT`, `UPDATE`, `DELETE`) fires
a database trigger that appends a row to a single `audit_log`
table. The conceptual columns:

```
audit_log
  id              UUID PRIMARY KEY
  tenant_id       UUID NOT NULL
  user_id         UUID                       -- the SET LOCAL user (see ADR-0036)
  table_name      TEXT NOT NULL
  operation       TEXT NOT NULL              -- 'INSERT' | 'UPDATE' | 'DELETE'
  record_id       UUID NOT NULL
  changed_fields  JSONB                      -- per-column before / after
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

`audit_log` is itself tenant-scoped and RLS-policed: each tenant
sees only its own audit entries. Inserts always succeed (the
trigger is not blocked by an outer query's RLS context, but the
inserted row carries the active `app.tenant_id`). The trigger
fires in `AFTER` form so a query that RLS rejects never produces
an audit entry — that case is rejected before the trigger runs and
is logged at the application level instead, with a `WARNING`.

### 8. Sentinel tenant in Phase 2

Phase 2 is structurally multi-tenant but functionally single-tenant.
On bootstrap, a sentinel tenant is created with a fixed UUID
documented in code (e.g.
`00000000-0000-0000-0000-000000000001`). The sentinel user defined
in ADR-0036 belongs to that tenant. Every test fixture and
integration scenario in Phase 2 runs against the sentinel tenant.
The Multi-Tenant code paths are exercised from day one — no
"single-tenant code" lives in the codebase.

### 9. Tenant onboarding is a Phase-5 implementation, not a schema concern

Creating a new tenant — provisioning the row in `tenants`, seeding
the lookup tables for that tenant, creating the first admin user,
sending an invitation, configuring tenant-specific theming —
is a Phase-5 workflow. The schema this ADR fixes already supports
all of those operations. Onboarding's UX, automation, and self-
service vs. operator-driven character are out of scope here.

## Rationale

- **Database-level enforcement over application-level filtering.**
  Defence in depth. Application bugs are not hypothetical; the
  database must be the lower bound on data confidentiality.
- **Strict variant over selective application of RLS.** Every
  exception is a place where the next query author has to remember
  to do something different. Uniformity removes that cognitive
  load and the bugs it produces.
- **`SET LOCAL` over a custom context mechanism.**
  `current_setting()` in RLS policies is the documented Postgres
  pattern, supported by every Postgres deployment, with a
  predictable lifecycle. A bespoke context mechanism would be more
  error-prone and harder to audit.
- **Within-tenant sharing as a separate table over per-resource
  ACL columns.** Sharing is sparse: most resources will be visible
  only to their owner or to the entire tenant. Storing ACLs as
  rows scales without bloating the resource tables themselves and
  keeps the tenant-scoped RLS policy on those tables simple.
- **Cross-tenant sharing forbidden at the policy level.** The
  cost of allowing it (audit complexity, ambiguous data-controller
  relationships under DSGVO, contractual entanglement) outweighs
  the benefit. Where a real cross-organisation flow is needed in
  the future, it is solved between deployments via documented
  integration, not by punching a hole in the isolation model.
- **Sentinel tenant in Phase 2.** Without it, the Phase-2 code path
  would have to special-case "no tenant context" — exactly the
  kind of conditional that later becomes a bypass. Running Phase 2
  against a real tenant from day one keeps the production code
  path uniform.
- **Audit triggers, not application-level audit calls.** A trigger
  cannot be forgotten by the next contributor writing a new write
  path. The trigger fires whether the write came from a repository
  method, a migration, or a manual `psql` session — which is what
  a regulator or auditor expects when they ask "how do you know
  every change was logged".

## Alternatives Considered

- **Application-layer tenant filtering without RLS.** Rejected.
  No defence in depth; one missing predicate is one cross-tenant
  leak. The application correctness assumption is exactly what RLS
  is designed not to require.
- **Schema-per-tenant.** Considered. Maximum DB-level isolation,
  no policies needed. Rejected because schema counts do not scale
  to SaaS deployments (hundreds of schemas per database is
  operationally awkward) and migrations have to be applied per
  schema. The structural preparation for multi-tenant SaaS in
  ADR-0033 rules this out as the default model.
- **Database-per-tenant.** Considered. Strongest isolation. Held
  open as an option for individual high-sensitivity customers who
  prefer or contractually require their own database (the
  repository pattern in ADR-0034 makes this a deployment
  choice, not a schema rewrite). Not the default because per-
  tenant database overhead is high for routine deployments.
- **Global, RLS-exempt lookup tables.** Considered. Avoids
  duplicate seed data per tenant. Rejected because special cases
  in query construction are bug-prone; the duplicate-seed cost is
  a one-time per-tenant operation, while the bug risk is
  permanent.
- **Cross-tenant sharing as a feature (a "marketplace" of shared
  investment definitions).** Rejected. Audit and DSGVO complexity
  outweigh the benefit at the foreseeable scale. The architecture
  is open to external integrations, which is the right place to
  handle that requirement when it arises.
- **Application-only audit log (no triggers).** Rejected. A future
  contributor writing a new write path could omit the audit call;
  the audit guarantee should not depend on memory.

## Consequences

### Positive

- Tenant isolation is technically enforced at the database level,
  independently of application code correctness.
- The audit story for institutional customers is concrete and
  verifiable: `pg_policies` shows the policies, the migration
  history shows when they were applied, and the `audit_log` table
  shows every write.
- Sharing inside a tenant is supported without weakening the
  cross-tenant boundary.
- Phase 2 runs the full multi-tenant code path from day one,
  avoiding the bug class that emerges when "Multi-Tenant" is
  added to a previously single-tenant code base.
- Defence in depth: two independent layers (RLS + application)
  both have to fail for a leak to happen.

### Negative

- The application must reliably set `app.tenant_id` for every
  database operation. A forgotten setup either errors loudly (RLS
  rejects the operation) or silently returns empty results (if
  the unset path is reached). Tests must cover both.
- Lookup tables are seeded per tenant; managing reference-data
  updates across many tenants is more work than maintaining a
  single global lookup. A periodic seed-update migration mechanism
  is the practical solution; designed when the first such update
  appears.
- Every new domain table must remember to ENABLE / FORCE RLS and
  attach the standard policy. A schema-review checklist is the
  enforcement mechanism; an automated verification step (e.g. a
  test that asserts every table in the public schema has RLS
  enabled) is a strongly-recommended follow-up.
- Tests that run as the Postgres superuser (or any role with
  `BYPASSRLS`) skip RLS entirely. Such tests would mask exactly the
  bugs RLS is meant to catch. The repository test suite must run
  under an unprivileged role; this is a CI configuration item.
- RLS imposes a small per-query overhead compared to plain SQL.
  At expected scale this is negligible; at SaaS scale it remains
  acceptable.

### Neutral / Follow-ups

- A schema-level test that walks `pg_class` / `pg_policies` and
  asserts every domain table has `tenant_isolation` enabled is a
  good regression guard. Strongly recommended.
- A periodic check that `audit_log` row counts grow consistently
  across active tenants is a useful operational signal.
- Tenant onboarding workflow (provisioning, seed-data installation,
  initial admin user invitation) lands in Phase 5.
- Tenant deletion / data-export workflows (DSGVO Art. 17, Art. 20)
  are also Phase-5 concerns that this ADR's schema accommodates.

## Implementation Notes

- **Standard policy helper.** A PL/pgSQL function
  `apply_tenant_rls(table_name TEXT)` attached to the schema is
  invoked from every Alembic migration that creates a domain
  table. Hand-written variations require an ADR-level note.
- **Connection-context wrapper.** A repository-layer context
  manager (or DI dependency, depending on the choice in
  ADR-0034's implementation) is the only sanctioned way to obtain
  a tenant-scoped session. The wrapper performs `SET LOCAL
  app.tenant_id = <uuid>` immediately on session acquisition.
  Direct session acquisition is forbidden and caught by code
  review or by a lint rule (the latter is a follow-up).
- **Test database role.** Tests run under a non-superuser role
  that does not have `BYPASSRLS`. The role is created by the
  migration that bootstraps the test database. Tests using the
  superuser role are restricted to schema-management scenarios
  (creating roles, applying migrations) and never to data
  operations.
- **Sentinel-tenant constant.** The sentinel UUID lives in a
  named constant in `core/` (e.g.
  `core/tenant_constants.py::SENTINEL_TENANT_ID`). Bootstrap
  scripts and tests refer to it by name.
- **Audit-log retention.** Not fixed here. A retention policy is
  a deployment-level decision (regulated industries typically
  10 years; some scenarios shorter). The schema does not enforce
  a deletion policy; rolling deletion is added when the first
  retention requirement is concrete.
- **Verification view (optional, recommended):** a database view
  `tenant_isolation_status` selecting rows from `pg_policies`
  and `pg_class` for the public schema, which an auditor or
  operator can inspect to confirm every domain table is
  RLS-protected.

## Compliance & Audit Relevance

- **BAIT AT 7.2 / AT 7.3** and **VAIT Chapter 5 / 6.** Tenant
  separation is the central technical control these frameworks
  expect for multi-tenant systems. RLS activation is verifiable
  by reading `pg_policies`; the `audit_log` table provides the
  forensic trail BAIT/VAIT explicitly require for changes to
  data.
- **MaRisk AT 7.2 — Mandantentrennung in IT systems.** The
  combination of `tenant_id NOT NULL`, RLS-enforced read/write
  policies, and tenant-scoped audit logs delivers the
  Mandantentrennung MaRisk mandates.
- **DSGVO Art. 25 (Privacy by Design / by Default).** Tenant
  isolation is a structural property of the schema, not a
  configuration toggle. The default for any new table is
  isolated; making it global would require a deliberate, ADR-
  documented exception.
- **DSGVO Art. 32 (Security of Processing).** Defence in depth
  through database-level enforcement plus application-level
  checks. The audit log supports breach detection.
- **DSGVO Art. 17 / Art. 20 (right to erasure / portability).**
  Tenant-scoped data is straightforward to enumerate and export
  for an individual tenant; the implementation of those workflows
  lands in Phase 5 but is not blocked by the schema.
- **ISO 25010 quality attributes affected:** Confidentiality
  (tenant boundary), Maintainability (uniform pattern), Reliability
  (defence in depth), Security (database-level enforcement of an
  application-level requirement).
- **DORA.** Data isolation is a precondition for the operational-
  resilience expectations DORA names; without it, an incident in
  one tenant would have an undefined blast radius.
- **Audit evidence:**
  - `pg_policies` query showing `tenant_isolation` policies on every
    domain table.
  - The Alembic migration history showing when each policy was
    applied.
  - The standard PL/pgSQL helper, code-reviewed and unchanged
    across migrations.
  - The `audit_log` table contents over time.
  - The repository-test CI run under the unprivileged role.

## References

- ADR-0001 (Layered Architecture) — repository layer is where the
  tenant-context wrapper lives.
- ADR-0018 (Planned Service / Repository Layering) — the layering
  this ADR's tenant-context discipline rides on.
- ADR-0019 (Planned Multi-User Readiness) — the conceptual
  precondition.
- ADR-0033 (Web Migration: Architectural Shift) — frame.
- ADR-0034 (Persistence Backend: Postgres) — substrate; binds
  `tenant_id NOT NULL` at the schema level.
- ADR-0036 (Authentication Strategy) — sets the user identity
  whose tenant binding drives `SET LOCAL app.tenant_id` per
  request.
- Postgres documentation, Row Security Policies
  (https://www.postgresql.org/docs/current/ddl-rowsecurity.html) —
  external reference for the underlying mechanism.

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-05-03 | PortfoliFLOW project owner   | Initial draft. Records the multi-tenant isolation model: `tenant_id` on every table, Postgres RLS policies as the enforcement mechanism, intra-tenant sharing via `resource_permissions`, hard cross-tenant boundary, audit logging via triggers, and the sentinel-tenant approach for Phase 2. |
| 2026-05-03 | PortfoliFLOW project owner   | Status moved to **Accepted**. Phase 1, stream B landed the RLS substrate: every domain table (`tenants`, `users`, `audit_log`, `data_store_entries`) has `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`. The `apply_tenant_rls(target_table TEXT)` PL/pgSQL helper attaches the standard `tenant_isolation` policy uniformly; `tenants` carries a separate `tenant_self_visibility` policy (a tenant can see and modify only its own row, structurally consistent with §1: new-tenant inserts are reserved for the superuser path until the Phase-5 onboarding workflow). The repository layer's `tenant_context` async context manager sets the tenant GUC via `SELECT set_config('app.tenant_id', :tid, true)` (ADR-0035 §4 wording said `SET LOCAL`; the implementation uses `set_config(..., true)` because asyncpg cannot bind parameters into a SET LOCAL statement — same transaction-local semantics, parameter-bindable). Repository tests B-01 through B-07 plus the bonus unset-context defence-in-depth check exercise the end-to-end isolation under the unprivileged `portfoliflow_app` role. The schema-regression-guard (`tests/regression/test_rls_schema_invariants.py`) verifies the invariants in CI; the app-role-permissions guard ensures `portfoliflow_app` never accidentally gains `BYPASSRLS` or `SUPERUSER`. Audit triggers wired on `users` write to `audit_log`; the trigger reads `app.user_id` via `current_setting(..., true)` and produces NULL in Phase 1 (no auth) — Phase 2's auth middleware will populate the GUC additively. Decider: PortfoliFLOW project owner. |
| 2026-05-10 | PortfoliFLOW project owner   | Translated residual German passages to English per ADR-0008 (Phase-6 Block 0c). No substantive change; status, decisions, and content unchanged. |
