# ADR-0078: Enforce RLS in `tenant_context` via Application-Role Switch Under Privileged Connections

- **Status:** Accepted
- **Date:** 2026-06-05
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** N/A (multi-tenant isolation correctness fix; relates to ADR-0035 RLS design, ADR-0040 superuser/app-role split, ADR-0063/0064 multi-tenant activation & super-admin CLI, ADR-0077 per-tenant seed parity)
- **Tags:** multi-tenancy, rls, tenant-isolation, provisioning, security, governance

---

## Context

PortfoliFLOW enforces tenant isolation through PostgreSQL Row-Level
Security (ADR-0035). Every domain table carries a `tenant_isolation`
policy that filters rows by `current_setting('app.tenant_id')::uuid`.
The repository layer relies **solely** on RLS for tenant scoping: read
methods carry no explicit `tenant_id` predicate. For example,
`AssetClassRepository.get_by_code` runs `WHERE lower(code) = :code` with
no tenant filter, and its docstring states "Cross-tenant rows are
invisible (RLS hides them)."

`tenant_context` (`core/repositories/_session.py`) is the sanctioned way
to obtain a tenant-scoped session. It sets the `app.tenant_id` GUC (and
optionally `app.user_id`, `app.is_super_admin`) on the transaction, but
it does **not** switch database role.

There are two database roles (ADR-0040):

- `portfoliflow_app` — the unprivileged application role used by the web
  app / GUI / bot via `DATABASE_URL`. RLS is **enforced** for it.
- the PostgreSQL **superuser** used by the CLI via
  `DATABASE_URL_SUPERUSER`. PostgreSQL **never enforces RLS for a
  superuser** — `FORCE ROW LEVEL SECURITY` and `row_security = on` do not
  change this.

The consequence is a silent isolation defect: when `tenant_context` is
opened on the superuser engine (the CLI path), the `app.tenant_id` GUC is
set but **no RLS policy ever evaluates it**, because the superuser
bypasses RLS. Every RLS-reliant repository read therefore sees rows from
**all** tenants.

This surfaced during tenant provisioning. `seed_tenant_defaults`
(ADR-0077) installs the `unclassified` asset class and the default
catalogue via `install_*` helpers whose existence checks call
`get_by_code`. Run from `portfoliflow create-tenant` on the superuser
engine, `get_by_code("unclassified")` returns the **primary tenant's**
row (created first, during bootstrap) rather than `None`, so the
installer logs "already present (no-op)" and skips. The freshly created
tenant receives none of its own seed rows. The web import, running as
`portfoliflow_app` with RLS enforced and scoped to the new tenant,
correctly finds nothing and raises. The same mechanism makes the SAA
seed configurations report "already present — skipped" for a brand-new
tenant.

This is not a defect in ADR-0077, which was implemented correctly;
ADR-0077 merely made the pre-existing hazard observable, and its claim
that "re-running `create-tenant` repairs the tenant idempotently" is
invalidated by this defect (the repair no-ops for the same reason).

The blast radius is broader than seeding: **any** tenant-scoped operation
performed through `tenant_context` on a privileged (RLS-bypassing)
connection silently operates across all tenants. This is a multi-tenant
isolation correctness bug, not a provisioning detail.

## Decision

`tenant_context` makes RLS enforcement true-by-construction regardless of
the connecting role: after setting the `app.*` GUCs, and gated by a new
parameter `enforce_rls: bool = True`, it switches the transaction to the
unprivileged application role for the remainder of the block:

```sql
SELECT set_config('role', :app_role, true);   -- transaction-local; auto-resets at COMMIT/ROLLBACK
```

`set_config('role', …, true)` is the bind-parameter-safe, transaction-
local equivalent of `SET LOCAL ROLE`, mirroring the existing
`app.tenant_id` pattern in `tenant_context` (asyncpg cannot bind into a
literal `SET LOCAL ROLE` command). The role name comes from configuration
(`APP_DB_ROLE`, default `portfoliflow_app`), never from caller input.

Rationale and seam:

- The web app already connects as `portfoliflow_app`; the role switch is
  a no-op there and changes nothing.
- On the superuser CLI engine, the switch makes tenant-scoped sessions
  behave **exactly** like the app: RLS is enforced, so every RLS-reliant
  repository scopes correctly — with **no changes to repository
  methods**.
- Cross-tenant **platform** operations (e.g. `create_tenant_idempotent`,
  the `login_audit` superuser writes) run on raw superuser connections
  **outside** `tenant_context` and are therefore unaffected.
- `super_admin_audit`'s RLS policy is GUC-based
  (`current_setting('app.is_super_admin', true) = 'true'`), so legitimate
  cross-tenant super-admin reads survive the role switch; `portfoliflow_app`
  already holds `SELECT/INSERT/UPDATE/DELETE` on all tables via the
  default-privileges grant in `db/init`.

This yields a clean conceptual model: **`tenant_context` = "act as this
tenant, under RLS"; a raw superuser connection = "platform-level
cross-tenant operation."** The `enforce_rls=False` escape hatch exists
only for an explicitly-audited cross-tenant caller that must bypass RLS
while inside a `tenant_context`; the expectation is that none, or very
few, need it, and each use must carry a justifying comment.

## Consequences

**Positive**
- Tenant isolation holds wherever `tenant_context` is used, independent
  of the connecting role. Closes the whole class of silent cross-tenant
  bleed, not just the seeding symptom.
- `seed_tenant_defaults` installs each new tenant's own seed rows; the
  ADR-0077 repair-by-re-run mechanism becomes actually correct.
- The superuser is reduced to what it should be: a platform-bootstrap /
  cross-tenant tool, with all tenant-scoped work RLS-enforced.

**Negative / costs**
- Every `tenant_context` caller now runs under `portfoliflow_app`
  privileges for the block. This requires the app role to hold the
  necessary table privileges — already true via the `db/init`
  default-privileges grant; verified, but a constraint to keep in mind
  for future tables.
- One new parameter on `tenant_context` and a one-time audit of all
  call sites to confirm none silently depended on superuser RLS-bypass
  inside a tenant context (the `enforce_rls=False` opt-out covers any
  that legitimately do).
- A future CLI role that is privileged-but-not-superuser would need
  membership in `portfoliflow_app` to `SET ROLE` to it; the current CLI
  uses an actual superuser, for which `SET ROLE` to any role is allowed.

**Neutral**
- No schema change, no migration, no API change. Web-app behaviour is
  unchanged (already the app role).

## Alternatives Considered

- **Explicit `tenant_id` predicate in every repository read (Option B).**
  Correct regardless of role and good defence-in-depth, but invasive
  across many methods and easy to omit on new methods — the isolation
  guarantee would live in N scattered call sites rather than one seam.
  Rejected as the primary fix; may be added later as targeted
  defence-in-depth on hot existence-check paths.
- **Pass `tenant_id` explicitly into the seed installers / existence
  checks (Option C).** Narrowest change, but fixes only the seed path and
  leaves the same latent hazard everywhere else `tenant_context` meets a
  privileged engine. Rejected as a band-aid.
- **Do nothing / document a manual remedy.** Rejected: it institutionalises
  a silent isolation defect and, in practice, invites manual SQL — a
  violation of a hard project principle.

## Compliance & Audit Relevance

Tenant (mandant) separation is a core control for a multi-tenant
institutional platform and is directly relevant to BAIT/VAIT separation
expectations. This change strengthens that control: it removes a path by
which a privileged connection could silently read or write across tenant
boundaries while believing itself tenant-scoped, and it makes the
superuser's role explicit and narrow. The audit trail is unaffected (the
b001 trigger and `super_admin_audit` continue to function; the latter's
GUC-based policy is preserved). No regulatory claim changes.

## Revision History

- 2026-06-05 — Initial version. Records the decision to enforce RLS in
  `tenant_context` by switching to the application role under privileged
  connections, fixing a silent cross-tenant isolation defect that caused
  per-tenant seeding to no-op for every non-primary tenant. Supersedes
  the "re-run repairs the tenant" repair-mechanism claim in ADR-0077
  (ADR-0077's body is left unchanged per the immutability principle; this
  note records the correction). Status: Accepted.
