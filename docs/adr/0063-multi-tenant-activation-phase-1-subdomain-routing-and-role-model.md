# ADR-0063: Multi-Tenant Activation (Phase 1) — Subdomain Routing and Role Model

- **Status:** Accepted
- **Date:** 2026-05-26
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, multi-tenant, authentication, security, authorisation, role-model

---

## Context

ADR-0035 committed PortfoliFLOW to a multi-tenant architecture
enforced through Postgres Row-Level Security. ADR-0036 added the
Phase-2 authentication surface (sessions, login audit, Argon2id local
passwords) with a deliberately deferred tenant-resolution hook —
`LocalPasswordAuthBackend._resolve_tenant` returns `SENTINEL_TENANT_ID`
unconditionally, and `web.auth.get_optional_session` opens its
session lookup inside `tenant_context(engine, SENTINEL_TENANT_ID)`.
The substrate is multi-tenant; the entry points are not.

Phase 6 Block 2 closes that gap. The roadmap item B1 frames it as
"Multi-User & Permissions"; the technical reality is broader. The
following must change together to leave a coherent stand:

1. **Tenant resolution at login.** The Phase-2 stub
   (`return SENTINEL_TENANT_ID`) has to become a real mapping from
   request properties (host, email, or explicit input) to a tenant
   id, and the mapping must be expressible in tests and reviewable
   by an auditor.
2. **Tenant resolution per request.** Once a session cookie is
   present, the application currently looks up the session row
   inside `tenant_context(engine, SENTINEL_TENANT_ID)` — i.e. it
   asks RLS to filter against the sentinel tenant before it knows
   which tenant the session belongs to. With multiple tenants, that
   single hard-wired call is incorrect by construction: a session
   belonging to tenant B is invisible when the lookup runs in
   tenant A's RLS context.
3. **Role model inside a tenant.** ADR-0036 §2 introduced
   `is_tenant_owner: bool` as a Phase-2 approximation. Roadmap B1
   leaves the decision open (D2: Owner/Member vs.
   Owner/Member/Viewer vs. per-resource sharing). Without a real
   role model, every authenticated user in a tenant currently has
   the same effective rights — which is unacceptable for the BAIT
   AT 7.2 / MaRisk AT 7.2 expectation of differentiated Identitäts-
   und Berechtigungsmanagement.
4. **A platform-level role separate from the tenant role.** The
   operator of a multi-tenant SaaS deployment (Minathena Capital
   running portfoliflow.net) and the operator of an
   on-premise install (a Versorgungswerk's sysadmin running
   PortfoliFLOW internally) both need a *platform* role that can
   create tenants, deactivate them, reset an owner account — and
   that role must not have ambient access to any tenant's data.

The Phase-2 ADR (0036 §3) sketched the auth-backend abstraction
that makes OIDC additive in Phase 5; it did not commit to a
specific tenant-resolution strategy. ADR-0036 §3 listed
"email-domain mapping or explicit subdomain dispatch" as the
candidate mechanisms for Phase-5 readiness. This ADR resolves that
choice.

This decision is squarely security-, audit-, and compliance-
relevant. BAIT AT 7.2 (Identitäts- und Berechtigungsmanagement),
VAIT Chapter 7, MaRisk AT 7.2 (Mandantentrennung + Berechtigungen),
DSGVO Art. 25/32 (Privacy by Design, Security of Processing), ISO
25010 (Security: Authenticity, Authorisation; Confidentiality;
Maintainability), and DORA (Operational Resilience: identity
controls) all bear on it. The Compliance & Audit Relevance section
is correspondingly substantial.

## Decision

### 1. Tenant resolution by subdomain

PortfoliFLOW resolves the active tenant from the request's `Host`
header. Each tenant carries a unique subdomain registered in a new
column `tenants.subdomain: TEXT UNIQUE NOT NULL`; resolution is
`tenants WHERE subdomain = host.split('.')[0]`.

A pluggable `TenantResolver` abstraction in
`services/tenant_resolution/` mediates the lookup. Phase-1 ships
one production implementation, `SubdomainTenantResolver`, and one
test/dev implementation, `ExplicitHostHeaderResolver` (the test
suite sets `Host: <slug>.portfoliflow.net` directly; no DNS
required). Future implementations (email-domain mapping, explicit
tenant picker) slot in alongside without rewriting consumers.

Local development uses the same mechanism: a `LOCAL_DEV_TENANT_SUBDOMAIN`
environment variable in `.env` instructs the resolver to treat
plain-`localhost` requests as belonging to the named tenant. This
removes the previous Phase-2 special case (`SENTINEL_TENANT_ID`
returned unconditionally) without breaking the developer experience.

`LocalPasswordAuthBackend.authenticate()` is changed to accept the
already-resolved `tenant_id: UUID` as an explicit argument from the
login route. The `_resolve_tenant` stub is removed. The route
(`web.routes.login::login_submit`) calls the `TenantResolver` from
the request, gets the tenant id, and passes it to the backend. The
backend does **not** call the resolver itself — the route owns that
boundary, and the backend stays purely about credential
verification.

Failure mode: a `Host` header that does not match any tenant's
subdomain (or a missing `LOCAL_DEV_TENANT_SUBDOMAIN` in development)
results in HTTP 404 from the login route, **before** any credential
verification runs. No login audit row is written for unresolved
hosts — there is no tenant context to scope the row to, and
NULL-tenant audit rows are reserved for unresolved-email attempts
within a known tenant.

### 2. Role model — Owner, Member, Auditor + platform-level Super-Admin

Two orthogonal axes:

**Tenant role.** `users.roles: TEXT[] NOT NULL` with a `CHECK`
constraint restricting values to `{'owner', 'member', 'auditor'}`
and `array_length(roles, 1) >= 1`. A user can carry one or several
tenant roles. The `is_tenant_owner: BOOLEAN` column from ADR-0036
§2 is migrated: existing rows with `is_tenant_owner = TRUE` become
`roles = ARRAY['owner']`; the column is then dropped.

Tenant-role semantics:

| Capability                                          | Owner | Member | Auditor |
|-----------------------------------------------------|:-----:|:------:|:-------:|
| Read domain data (investments, NAVs, cashflows)     |   ✓   |   ✓    |    ✓    |
| Write domain data (Excel import, manual edits)      |   ✓   |        |         |
| Run analytics (SAA, Portfolio Review, Benchmarks)   |   ✓   |   ✓    |    ✓    |
| Persist analytics results (SAA optimisations, ...)  |   ✓   |   ✓    |         |
| Generate exports (PDF, Excel reports)               |   ✓   |   ✓    |    ✓    |
| Manage users within the tenant                      |   ✓   |        |         |
| Change tenant settings                              |   ✓   |        |         |
| Read tenant-scoped `audit_log` (full)               |       |        |    ✓    |
| Read tenant-scoped `login_audit` (full)             |       |        |    ✓    |

The Auditor role is the differentiated read-only auditor seat
institutional reviewers expect. It deliberately does not include
domain-write rights; it deliberately includes broad read rights
(including the audit log itself, scoped to the tenant). Owner does
**not** have ambient full-audit-log access — separation of
operational and audit roles is structurally enforced.

**Platform role.** `users.is_super_admin: BOOLEAN NOT NULL DEFAULT
FALSE`. A CHECK constraint binds `is_super_admin = TRUE` to
`tenant_id = SYSTEM_TENANT_ID` so super-admins structurally cannot
co-exist with tenant data:

```sql
CHECK ((is_super_admin = FALSE) OR (tenant_id = '00000000-0000-0000-0000-000000000000'::uuid))
```

The full super-admin surface is the subject of ADR-0064; this ADR
binds the schema invariant.

### 3. The system tenant

A second hardcoded tenant constant joins `SENTINEL_TENANT_ID`:

```python
SYSTEM_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000000")
```

The system tenant has `subdomain = 'admin'`. Its sole purpose is
to host super-admin user accounts (one or several). It is *not* a
data-bearing tenant: no investments, no SAA models, no reports
live there. A regression test asserts that the system tenant holds
zero rows in every domain table other than `users`.

The migration that introduces the schema changes seeds the system
tenant row idempotently (`INSERT ... ON CONFLICT (id) DO NOTHING`),
mirroring the b008 pattern for the sentinel tenant.

### 4. Session lookup before tenant context

`web.auth.get_optional_session` is rewritten to resolve the tenant
from the session token *before* opening a tenant-scoped session.
The session-token lookup runs against the **audit engine** (the
Postgres-superuser-bound engine already used exclusively for
`login_audit` inserts per ADR-0036 §8). The query reads only the
minimum required fields:

```sql
SELECT id, tenant_id, user_id, expires_at, last_seen_at, csrf_token
FROM sessions
WHERE session_token = :token
  AND expires_at > NOW()
  AND last_seen_at + INTERVAL '8 hours' > NOW()
```

Once `tenant_id` is known, the request proceeds via the normal
`tenant_context(engine, tenant_id, user_id=...)` path. The audit
engine is **never** used for anything beyond (a) `login_audit`
inserts and (b) this session-token resolve.

A regression test (`tests/regression/test_audit_engine_usage.py`,
renamed from the existing `test_audit_engine_only_writes_login_audit.py`)
enforces both invariants by walking the source tree and asserting
the audit engine is used in exactly the two named code paths.

The session-token resolve is a hot path (every authenticated
request); the query hits the existing
`uq_sessions_session_token` unique index, so per-call cost is one
B-tree lookup plus the asyncpg round-trip. No additional cache is
introduced in Phase 1; if profiling shows the round-trip dominating
request latency at expected load, a short-TTL in-memory LRU
(`session_token → (tenant_id, user_id, expires_at)`) is the
follow-up.

### 5. Permission overlay (preview, full detail deferred)

The Phase-1 schema additions (`roles: TEXT[]`, `is_super_admin:
BOOLEAN`) are the data substrate. The route-level enforcement is
expressed via two new FastAPI dependencies:

- `require_role(*allowed)` — checks the authenticated user's
  `roles` array against the allowed set, raises 403 on mismatch.
- `require_super_admin` — checks `is_super_admin = TRUE`, raises
  403 otherwise.

Each existing mutating route in the FastAPI surface gets the
appropriate dependency in this ADR's implementation. The mapping
follows the role table in §2 above:

- Domain-write routes (Excel import, manual investment edits, user
  management) gain `Depends(require_role('owner'))`.
- Analytics-write routes (SAA optimisation persistence, Portfolio
  Review snapshot persistence, Limit-Set persistence) gain
  `Depends(require_role('owner', 'member'))`.
- Read-only routes remain protected by `Depends(require_session)`
  only.

The Tool-Trust-Classes overlay (ADR-0022 §4) — which sub-classifies
`WRITE_INTERNAL` tools by per-role permission — is a separate
roadmap item (B1c) and a separate ADR. This ADR notes the planned
direction; it does not commit it.

### 6. Migration path — no data carry-over

PortfoliFLOW currently runs against test data only. The
multi-tenant activation does **not** migrate any existing rows.
Operators perform a `portfoliflow reset-dev`-style truncate (or
equivalent on a fresh Postgres container) and re-bootstrap; the
Alembic migration that introduces the schema changes does *not*
include a data-migration step.

The Alembic migration does:

1. Add `tenants.subdomain TEXT UNIQUE NOT NULL` with a partial-fill
   via the seeded sentinel + system tenant rows (Minathena Capital
   gets subdomain `'minathena-capital'`; the system tenant gets
   subdomain `'admin'`).
2. Add `users.roles TEXT[] NOT NULL DEFAULT ARRAY['member']::text[]`
   with the value-restriction CHECK.
3. Backfill `users.roles` from `is_tenant_owner` (`TRUE` → `ARRAY['owner']`,
   else `ARRAY['member']`), then drop `is_tenant_owner`.
4. Add `users.is_super_admin BOOLEAN NOT NULL DEFAULT FALSE` with
   the system-tenant CHECK.
5. Insert the system tenant row idempotently.
6. Rename the sentinel tenant from `'Sentinel Tenant'` to
   `'Minathena Capital'` and set `subdomain = 'minathena-capital'`.
   The UUID `SENTINEL_TENANT_ID` is *retained* — code references
   stay valid, only the semantic meaning sharpens.
7. Add the `super_admin_audit` table (full detail in ADR-0064).

### 7. Backwards-compatibility hooks intentionally removed

The Phase-2 `SENTINEL_TENANT_ID` constant is **renamed** to
`PRIMARY_TENANT_ID` (with an alias retained for one release for
incremental refactor) and its semantic meaning narrows to "the
hardcoded UUID of the Minathena Capital tenant". The
"sentinel" framing — "single tenant standing in for the future
many" — no longer applies and removing the name removes a class of
mental confusion.

The TODO(P6-A) markers in `services/auth/local_password.py`,
`web/auth.py`, and `services/tools/_tool_context.py` are resolved
by the implementation of this ADR and are removed.

## Rationale

- **Subdomain routing over email-domain mapping.** Subdomain
  routing aligns with the institutional UX expectation (each
  organisation gets its own URL), avoids the collision risk of
  generic email domains (`@gmail.com`, `@outlook.com`), and uses
  the DNS infrastructure already being established for the hosted
  deployment (DNS for `*.portfoliflow.com` plus the marketing
  site). Email-domain mapping is preserved as a future option
  behind the `TenantResolver` abstraction; it is not foreclosed.
- **`TEXT[]` over `enum` for roles.** Postgres enums require a
  schema migration for every new value; arrays accept new values
  by `CHECK`-constraint update. A user can also hold multiple
  roles natively (`['owner', 'auditor']`) without a junction
  table — relevant for small teams where one person wears several
  hats during a transition. The CHECK constraint provides the
  same data-integrity guarantee as an enum.
- **Auditor as a first-class role.** Differentiated auditor
  access is a BAIT/VAIT expectation and easier to introduce
  cleanly now than to retrofit later. Owner does not get full
  audit-log access by default; that separation matches the
  Mandantentrennung principle of MaRisk AT 7.2.
- **Super-admin as orthogonal axis (`is_super_admin: BOOLEAN`),
  not a role value.** A super-admin is *not* an extreme form of
  owner — it is a platform-level role with no ambient tenant
  membership. Conflating it into `roles = ['super_admin']` would
  invite confusion ("can a super-admin also be a tenant owner?")
  and break the structural guarantee that super-admins cannot
  hold tenant data. The orthogonal axis with the
  system-tenant CHECK encodes the invariant in the schema.
- **Audit engine for session lookup, not a view or JWT.** Reusing
  the existing audit-engine asymmetry is the minimal change with
  the smallest new exception surface. A bypass view would
  introduce a second RLS exception in the schema (the only
  current exceptions are the three allow-listed global tables);
  JWT would break ADR-0036 §1's deliberate choice of
  server-side state and lose logout-everywhere / idle-reset
  semantics.
- **No data migration.** The operator commitment ("everything is
  test data, we can re-bootstrap") removes an entire class of
  retrofit risk and lets the Alembic migration stay focused on
  schema changes. A future data-migration ADR is the right place
  if and when real production data ever needs to be re-shaped.

## Alternatives Considered

- **Email-domain tenant mapping** (Alt-A). Resolution via
  `email.split('@')[1] → tenant_email_domain`. Rejected as the
  primary mechanism: collisions on shared domains
  (`gmail.com` for hobby/trial accounts) plus the user-confusion
  cost of "I changed my email so my tenant changed". Preserved
  as a future implementation behind `TenantResolver`.
- **Explicit tenant picker on the login page** (Alt-B). The
  login URL is shared (`portfoliflow.net/login`); the form asks
  for tenant slug + email + password. Rejected as the primary
  UX: harder to bookmark, exposes the existence and names of
  tenants on a single public surface (a mild information
  disclosure), and gives no natural seam for tenant-specific
  branding.
- **Single `role TEXT` enum column** (Alt-C). Rejected: cannot
  represent multi-role users (Owner+Auditor for a small team)
  without a workaround, and every new role requires an enum
  migration. The CHECK-constrained TEXT[] gives the same
  integrity guarantee with greater additive flexibility.
- **Super-admin as `roles = ['super_admin']` value** (Alt-D).
  Rejected: invites the question "can a super-admin also be a
  tenant owner?" and would require either explicit conflict
  logic or a structurally inconsistent state. The orthogonal
  axis (`is_super_admin: BOOLEAN` + system-tenant CHECK) makes
  the invariant readable from the schema alone.
- **JWT-based stateless sessions** (Alt-E). Rejected: cannot
  invalidate a token on logout or password rotation without a
  blocklist (which is just sessions-without-rows-again); breaks
  ADR-0036 §1's server-side-state decision; OWASP guidance is
  against JWT for browser session management.
- **Bypass view (`session_lookup`) for the pre-tenant session
  resolve** (Alt-F). Rejected: adds a second RLS-exception
  surface to the schema, where today's only exceptions are the
  three globally-shared lookup tables (allow-listed in the
  regression guard). The audit-engine path reuses an asymmetry
  that already has a regression-test invariant attached to it.
- **Keep `is_tenant_owner: BOOLEAN`, add `is_member`, `is_auditor`
  as parallel booleans** (Alt-G). Rejected: every new role would
  be a schema migration plus an N-boolean predicate. The TEXT[]
  scales additively.

## Consequences

### Positive

- The "single hardcoded tenant" assumption disappears from every
  application code path. After this ADR lands, adding the second
  real tenant is a CLI call (`portfoliflow create-tenant`), not
  a code change.
- Role-based authorisation is structurally available from Phase 1
  Block 2. BAIT/VAIT/MaRisk reviewers see a documented role
  model rather than a single-flag approximation.
- The auditor role is differentiated from the operator role,
  matching the separation-of-duties expectation of regulated
  industries.
- The super-admin role is structurally isolated from tenant
  data: a super-admin user **cannot** be a row in a
  Minathena Capital tenant. The CHECK constraint is the
  schema-level guarantee.
- The `TenantResolver` abstraction is the seam for the future
  options (email-domain, IdP-driven, picker) without further
  ADRs to the core code paths.
- The audit-engine usage is constrained to exactly two named
  code paths, each with a regression test guarding it.

### Negative

- Every authenticated request now pays one audit-engine query
  (the session-token resolve) before the tenant-scoped session
  begins. At expected load the cost is one B-tree hit on a unique
  index; at SaaS scale it may motivate an in-memory cache.
- The `SENTINEL_TENANT_ID` → `PRIMARY_TENANT_ID` rename touches
  many files (constants, tests, comments). A brief
  backward-compatibility alias mitigates the blast radius, but
  the codebase carries two names during the transition.
- Excel imports and manual edits become Owner-only. Trial-tenant
  setups where a Member needs to upload test data require either
  promoting the user to Owner temporarily or doing the upload
  via a different account. This is the intended access pattern;
  it may surprise users accustomed to "everyone can do
  everything".
- Test fixtures that previously assumed a single sentinel
  tenant need to set the `Host` header explicitly via the test
  resolver. The existing `tests/web/test_saa_rls.py` already
  follows this pattern; remaining tests will need adjustment.

### Neutral / Follow-ups

- **Tool-Trust per role (ADR-0022 §4 → roadmap B1c).** Needed
  before Shirley's write-capable tools (chart generation,
  potential automation tools) can be safely exposed to Members.
- **MFA.** ADR-0036 §4 already deferred this; the multi-tenant
  activation does not change the deferral. MFA remains the
  prerequisite for institutional production rollout.
- **OIDC.** ADR-0036 §3 prepared the auth-backend abstraction;
  the OIDC backend will fit alongside `LocalPasswordAuthBackend`
  without touching the `TenantResolver` abstraction.
- **DSGVO Art. 17 anonymisation.** Still a Phase-5 concern; the
  schema additions in this ADR do not block it.
- **Viewer role.** Not introduced now; trivially added later by
  adding `'viewer'` to the CHECK constraint and assigning
  read-only routes to allow it.
- **Per-resource permissions** (`resource_permissions` table from
  ADR-0035 §5). Conceptually prepared in ADR-0035; not introduced
  by this ADR. Future need-driven.
- **Hardening of the deployment.** Subdomain routing implies
  TLS for `*.portfoliflow.net` (wildcard certificate or
  per-subdomain provisioning). The C0 Hetzner-Hardening
  roadmap item owns this.

## Implementation Notes

- **Affected files (new):**
  - `services/tenant_resolution/__init__.py`,
    `services/tenant_resolution/resolver.py` (the abstraction +
    `SubdomainTenantResolver` + `ExplicitHostHeaderResolver` for
    tests),
  - `services/tenant_resolution/dependencies.py` (FastAPI
    dependency wiring),
  - `web/permissions.py` (the `require_role` /
    `require_super_admin` dependencies),
  - Alembic migration `b012_multi_tenant_activation.py`
    (subdomain column, roles array, is_super_admin, system
    tenant seed, sentinel rename),
  - `core/tenant_constants.py` extended with `SYSTEM_TENANT_ID`
    and `PRIMARY_TENANT_ID` (the new name for `SENTINEL_TENANT_ID`),
  - Regression tests
    (`tests/regression/test_audit_engine_usage.py`,
    `tests/regression/test_system_tenant_holds_no_domain_data.py`,
    `tests/regression/test_users_roles_invariants.py`),
  - End-to-end tests
    (`tests/web/test_subdomain_routing.py`,
    `tests/web/test_role_based_access.py`).

- **Affected files (modified):**
  - `services/auth/local_password.py` —
    `_resolve_tenant` removed, `authenticate(email, password,
    *, tenant_id, ...)` signature updated.
  - `web/auth.py` — `get_optional_session` rewritten to use the
    audit engine for the pre-tenant resolve; `require_session`
    unchanged in shape, but consuming the new resolution path.
  - `web/routes/login.py` — `login_submit` calls the
    `TenantResolver` first.
  - `web/main.py` — `create_app` wires the `TenantResolver`
    into `app.state`; the `_shell_processor`'s hardcoded
    `tenant_name = "Sentinel Tenant"` becomes a session-derived
    lookup.
  - `core/models/user.py` — `roles` column, `is_super_admin`
    column, `is_tenant_owner` dropped, CHECK constraints added.
  - `core/repositories/user_repository.py` — DTO and repository
    methods updated for the new columns.
  - `cli/bootstrap.py` — extended to seed the system tenant,
    rename the sentinel, accept a `SUPER_ADMIN_EMAIL` /
    `SUPER_ADMIN_PASSWORD` for initial super-admin creation
    (full detail in ADR-0064).
  - `core/tool_context.py` (and the chat-route call site) —
    the hardcoded `SENTINEL_TENANT_ID` becomes the resolved
    request tenant.

- **Removed surfaces:**
  - `services/auth/local_password.py::_resolve_tenant` (the
    sentinel-returning stub).
  - The hardcoded `SENTINEL_TENANT_ID` reference in
    `web/auth.py::get_optional_session`.
  - The hardcoded `SENTINEL_TENANT_ID` reference in the chat
    route's tool-context population.

- **Test discipline.** Cross-tenant isolation tests
  (`tests/web/test_saa_rls.py` is the template) are extended
  to cover the role distinctions — a Member cannot POST to
  domain-write routes even in their own tenant; an Auditor
  cannot run analytics-write routes; a Super-Admin cannot
  reach any tenant-data route at all.

- **Documentation.** `CLAUDE.md` glossary entries added:
  - **System tenant** — the structurally-anchored tenant that
    hosts super-admin user accounts; subdomain `admin`,
    `SYSTEM_TENANT_ID = 00000000-0000-0000-0000-000000000000`.
  - **Primary tenant** — the previous "Sentinel tenant"; now
    the production Minathena Capital tenant,
    `PRIMARY_TENANT_ID = 00000000-0000-0000-0000-000000000001`.
  - **Tenant resolver** — the pluggable mechanism that maps a
    request to a tenant; current implementation is
    `SubdomainTenantResolver`.
  - **Super-admin** — a user with `is_super_admin = TRUE`
    living in the system tenant; full surface in ADR-0064.

## Compliance & Audit Relevance

- **BAIT AT 7.2 / VAIT Chapter 7 — Identity and Access
  Management.** Role-based authorisation with a documented
  Owner/Member/Auditor distinction, plus a separate platform
  role, satisfies the differentiated-rights expectation
  these frameworks codify. The MFA gap remains (per ADR-0036)
  and is the dominant qualification before institutional
  production rollout.
- **MaRisk AT 7.2 — Mandantentrennung + Berechtigungen.** The
  subdomain-routed tenant boundary is a structural separation
  visible already at the URL layer; the role model gives the
  Berechtigungen axis. The Auditor role is the separation-of-
  duties seat MaRisk reviewers explicitly look for.
- **DSGVO Art. 25 (Privacy by Design).** Default for a new user
  is `roles = ['member']` — the minimum privilege. Owners are
  created deliberately (CLI bootstrap or Owner-driven admin
  UI), never by default.
- **DSGVO Art. 32 (Security of Processing).** Authorisation
  enforcement happens at the route layer (FastAPI
  dependencies) and is independent of RLS — defence in depth.
  An RLS misconfiguration would not silently leak across the
  role boundary; an authorisation bypass would not silently
  leak across the tenant boundary.
- **GoBD (Unalterability and Traceability).** The audit-log
  schema is unchanged; the role model means audit reviewers
  (the Auditor role) now have a structurally-distinct read
  path into it.
- **ISO 25010.** Security (Authenticity through tenant + user
  identification; Authorisation through role-based access),
  Confidentiality (tenant boundary plus role boundary inside),
  Maintainability (single-seam `TenantResolver` abstraction;
  array-based roles add cheaply), Reliability (the audit-
  engine usage is closed under two named paths with a
  regression test).
- **DORA (Operational Resilience).** Identity-control granularity
  with documented audit trail is part of the operational-
  resilience surface.
- **Audit evidence.**
  - Alembic migration `b012_multi_tenant_activation.py` showing
    the schema changes.
  - `pg_constraint` query showing the
    `ck_users_super_admin_in_system_tenant` and the
    `ck_users_roles_values` CHECK constraints.
  - `services/tenant_resolution/resolver.py` (the abstraction
    and the one production implementation), code-reviewed and
    test-covered.
  - The two regression tests guarding the audit-engine usage
    (`test_audit_engine_usage.py`) and the system-tenant
    isolation (`test_system_tenant_holds_no_domain_data.py`).
  - The end-to-end role-based-access tests
    (`tests/web/test_role_based_access.py`).
  - The `super_admin_audit` table contents over time
    (defined in ADR-0064; referenced from this ADR).

## References

- ADR-0019 (Planned Multi-User Readiness) — the precondition;
  this ADR closes its deferred questions on tenant resolution
  and role model.
- ADR-0022 (Tool Trust Classes and Gating Policy) — the
  single-trusted-user assumption named there; sub-classification
  of `WRITE_INTERNAL` is the follow-up roadmap item B1c.
- ADR-0033 (Web Migration: Architectural Shift) — frame.
- ADR-0034 (Persistence Backend: Postgres) — substrate.
- ADR-0035 (Multi-Tenant Architecture: Tenant Isolation via RLS)
  — the RLS substrate this ADR's `TenantResolver` and
  session-lookup path feed.
- ADR-0036 (Authentication Strategy: Session-Based with
  OIDC-Readiness) — Phase-2 substrate; §3's tenant-resolution
  hook is concretised here.
- ADR-0040 (Sentinel Bootstrap CLI-driven) — extended to multi-
  tenant in ADR-0064.
- ADR-0047 (Tool-Execution Context Propagation) — the
  `resolve_tenant_id()` seam is rewired to use the resolved
  request tenant.
- ADR-0064 (Super-Admin Surface) — companion to this ADR,
  covers the platform-role surface in full.
- OWASP Authorisation Cheat Sheet — referenced for the
  defence-in-depth (route guard + RLS) pattern.

---

## Revision History

| Date       | Author                       | Change                                                                                                          |
|------------|------------------------------|-----------------------------------------------------------------------------------------------------------------|
| 2026-05-26 | PortfoliFLOW project owner   | Initial draft. Proposed Phase-1 multi-tenant activation: subdomain routing, Owner/Member/Auditor role model, super-admin axis (full surface in ADR-0064), audit-engine-based pre-tenant session resolve, no-carry-over migration path. |
| 2026-06-03 | PortfoliFLOW project owner   | Status corrected to **Accepted** to match shipped code during doc/code reconciliation: migration `b012_multi_tenant_activation` (subdomain, `roles TEXT[]`, `is_super_admin`), `services/tenant_resolution/SubdomainTenantResolver`, role-based route guards live. The per-action tool-trust overlay (roadmap B1c) and tenant-owner user-management UI (B1f) remain separate open items. |
