# ADR-0064: Super-Admin Surface — CLI-Driven Platform Operations, No Web-Side Tenant-Data Access

- **Status:** Accepted
- **Date:** 2026-05-26
- **Deciders:** PortfoliFLOW project owner
- **Tags:** multi-tenant, super-admin, platform-operations, security, audit, cli

---

## Context

ADR-0063 introduces the platform-level `is_super_admin: BOOLEAN`
axis on `users`, structurally bound to a hardcoded `SYSTEM_TENANT_ID`
via a CHECK constraint. The schema invariant is established; the
*surface* — what a super-admin can actually do, how they log in,
and how their actions are audited — is the subject of this ADR.

The decision is shaped by an explicit non-functional requirement:

> *"It would not be conducive to a deployment with real customers
> if I, as platform operator, could see their data. If access is
> required, then only via CLI, and I would introduce a policy that
> this is never done unless the user permits it."*

That requirement rules out the web-side impersonation pattern
("act as tenant X to debug their data") that is common in
multi-tenant SaaS. It implies a stronger structural commitment:

- **No web route accessible to a super-admin shall ever return
  tenant-scoped domain data.** A super-admin opening the admin
  surface sees tenants as opaque entities (id, name, subdomain,
  user count, creation date, activation state) — never a list of
  investments, a NAV curve, an SAA configuration, or a portfolio
  review.
- **Tenant-data access in genuine emergencies happens via the
  CLI, executed on the server host.** This pathway exists, is
  documented, audited, and disabled in the web layer by
  construction (no FastAPI route reads tenant data while a
  super-admin's session is active).
- **Both the web-side platform-operations surface and the CLI
  emergency surface write to a dedicated audit table
  (`super_admin_audit`)** so the operator's own actions are
  reviewable to the same standard as tenant-side operations.

The institutional posture this codifies: "Even if I wanted to,
I structurally cannot open your data from the web UI. The CLI
backdoor exists only because emergencies demand it, every use is
permanently logged, and our operational policy is to never use it
without your explicit consent." That sentence is the audit answer
to the question every Versorgungswerk and FoF asks when evaluating
a hosted SaaS.

This decision is security-, audit-, and compliance-relevant. BAIT
AT 9 (outsourcing controls), DSGVO Art. 28/32 (processor controls,
security of processing), MaRisk AT 7.2 (Mandantentrennung), and
ISO 25010 (Confidentiality, Accountability) bear on it.

## Decision

### 1. Web surface — `admin.portfoliflow.net`

The system tenant's subdomain is `admin`. Requests with
`Host: admin.portfoliflow.net` (or `admin.<dev-domain>`) are
resolved by `SubdomainTenantResolver` (ADR-0063 §1) to the system
tenant. The login surface at `admin.portfoliflow.net/login` accepts
only users with `is_super_admin = TRUE` and `tenant_id =
SYSTEM_TENANT_ID`. Any login attempt against
`admin.portfoliflow.net` by a non-super-admin user produces the
generic "Invalid credentials" response — the existence of the
distinction is not surfaced.

The admin surface exposes the following routes only:

- `GET /super-admin/tenants` — list of all tenants
  (id, name, subdomain, created_at, is_active, user_count). Read
  via the audit engine (RLS bypass) since the super-admin is not
  scoped to any single tenant. The result set deliberately
  excludes any field that could leak tenant-data shape (e.g. no
  "number of investments" — that already starts shaping a
  business-data signal).
- `POST /super-admin/tenants` — create a new tenant (name,
  subdomain, initial owner email + temporary password). Idempotent
  on subdomain. Writes via audit engine; writes one row each to
  `super_admin_audit` and the new tenant's own initial state.
- `POST /super-admin/tenants/{id}/deactivate` — set
  `tenants.is_active = FALSE`. A deactivated tenant cannot be
  logged into; existing sessions are invalidated. Writes
  `super_admin_audit`.
- `POST /super-admin/tenants/{id}/reactivate` — inverse.
- `POST /super-admin/tenants/{id}/reset-owner` — reset the
  password of the tenant's owner account. Used when the owner
  has lost access. Generates a single-use rotation token (out
  of scope for this ADR; the Phase-2 substitute is the operator
  manually communicating a new temporary password). Writes
  `super_admin_audit`.
- `GET /super-admin/users` — list of super-admin users in the
  system tenant. Used to add or deactivate super-admins.
- `POST /super-admin/users` — create another super-admin user.
- `POST /super-admin/users/{id}/deactivate` — set `is_active =
  FALSE` on a super-admin user.

A regression test
(`tests/regression/test_super_admin_routes_no_tenant_data.py`)
walks the `/super-admin/*` route surface and asserts that no
handler reads from any tenant-data table (investments,
investment_navs, investment_cashflows, saa_configurations,
portfolio_review_snapshots, …). The list of forbidden tables is
maintained alongside the regression test; adding a new domain
table to the project is accompanied by adding it to the
forbidden list.

The `tenants` table reads in the super-admin routes go via the
audit engine, because the super-admin's request is not in a
tenant scope. The audit engine is the existing
RLS-bypass mechanism; this ADR extends its sanctioned usage from
"`login_audit` writes + session-token resolve" (ADR-0063 §4) to
"...+ system-tenant operations". The audit-engine usage regression
test (`tests/regression/test_audit_engine_usage.py`) is extended
to cover this third sanctioned path.

### 2. Authentication for super-admins

Super-admins use the same `LocalPasswordAuthBackend` as tenant
users, with the same Argon2id parameters, the same account-lockout
policy (5 failed attempts within 15 minutes), the same session
cookie (`portfoliflow_session`), and the same CSRF discipline.
The differences are entirely in *which routes the resulting
session can reach* — the auth backend itself is unchanged.

The `LocalPasswordAuthBackend.authenticate` call from the admin
login route passes `tenant_id = SYSTEM_TENANT_ID`. The backend
resolves the user, verifies the password, records the login in
`login_audit` (with `tenant_id = SYSTEM_TENANT_ID`), and returns
the `UserDTO`. The login route then verifies `user.is_super_admin
= TRUE` and `user.tenant_id = SYSTEM_TENANT_ID` *both* — defence
in depth against a hypothetical schema violation. If either check
fails, the login is rejected with the generic error and the
attempt is logged.

MFA absence applies to super-admin accounts the same way it
applies to tenant accounts (ADR-0036 §4). The implication is
sharper here — a super-admin credential compromise has a larger
blast radius — and the C0 Hetzner-Hardening roadmap item lists
super-admin MFA as a prerequisite to the first production rollout
with external customer tenants, alongside general MFA.

### 3. CLI surface — emergency platform operations

The full platform-operations vocabulary is available via CLI
subcommands, running as the Postgres superuser:

- `portfoliflow create-tenant --name --subdomain --owner-email
  [--owner-password-stdin]` — idempotent on subdomain. Same
  internals as the web-side `POST /super-admin/tenants` but
  invokable without a web surface (useful for initial bootstrap
  and for CI / deployment automation).
- `portfoliflow create-super-admin --email
  [--password-stdin]` — idempotent on email. Creates a user in
  the system tenant with `is_super_admin = TRUE` and
  `roles = ARRAY['owner']` (the role array is required to satisfy
  the `roles NOT NULL, array_length >= 1` constraint; for
  super-admins the value is conventional, not semantically
  meaningful at the route layer — every super-admin route gates
  on `is_super_admin = TRUE` directly, not on `roles`).
- `portfoliflow create-user --tenant <subdomain-or-uuid>
  --email --roles owner,member,auditor [--password-stdin]` —
  idempotent on `(tenant_id, email)`. Used by operators to create
  the initial owner of a new tenant alongside `create-tenant`,
  and by recovery scenarios.
- `portfoliflow inspect-tenant --tenant <subdomain-or-uuid>
  --reason "<text>"` — the **emergency tenant-data read path**.
  Opens a read-only `tenant_context()` against the requested
  tenant, runs a structured diagnostic report (tenant metadata,
  user list, investment count by asset class, latest data-import
  timestamp, audit-log size), and prints it to stdout. The
  `--reason` argument is mandatory: every invocation writes one
  row to `super_admin_audit` *and* one row to the target
  tenant's `audit_log` capturing operator identity (resolved
  from the `SUPER_ADMIN_EMAIL` env var, OS user, or an explicit
  `--operator` flag), the reason text, the timestamp, and the
  command output digest. The command is **read-only by
  construction**: every SQL statement it issues is a `SELECT`,
  and an integration test
  (`tests/cli/test_inspect_tenant_is_read_only.py`) asserts no
  `INSERT`/`UPDATE`/`DELETE` is issued against any tenant-data
  table.

The CLI does not provide a `mutate-tenant-data` or
`run-as-tenant-owner` subcommand. There is no documented or
sanctioned way to *write* tenant-scoped domain data from the
super-admin surface. If a tenant's data must be modified for
recovery, the operator coordinates with the tenant owner; the
owner either provides corrected data via the normal Excel-import
path or grants temporary owner-level access to an emergency user
account that the super-admin creates (via `create-user` into the
target tenant, with the tenant owner's explicit out-of-band
consent recorded in the operational journal). The structural
guarantee — "super-admin cannot write tenant data" — is preserved.

### 4. `super_admin_audit` table

A new tenant-scoped table where `tenant_id` may be either the
system tenant (for platform-wide actions) or a target tenant (for
actions targeting a specific tenant):

```
super_admin_audit
  id                    UUID PRIMARY KEY
  super_admin_user_id   UUID NOT NULL REFERENCES users(id)
  action                TEXT NOT NULL
  target_tenant_id      UUID NULL REFERENCES tenants(id)
  target_user_id        UUID NULL REFERENCES users(id)
  reason                TEXT NULL
  payload               JSONB NULL
  ip_address            INET NULL
  user_agent            TEXT NULL
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

RLS policy: only super-admin users (`is_super_admin = TRUE`) can
read; the policy is expressed against `app.is_super_admin` (a new
GUC set alongside `app.tenant_id` and `app.user_id` in the
super-admin request path) rather than against `app.tenant_id`,
because super-admin audit rows cross tenant boundaries by design.
Writes go via the audit engine; no application-level RLS bypass
is required because the audit engine bypasses RLS structurally.

An auditor in a target tenant (with `roles && ARRAY['auditor']`)
**does not** see `super_admin_audit` rows directly. Instead, the
mirror row written by `inspect-tenant` to the *target tenant's*
`audit_log` is what the tenant's auditor sees. The schema-level
separation matches the trust-model separation: super-admin actions
are reviewable by super-admins (and by external compliance review
of the operator's own books); the *fact* that a super-admin
touched a tenant is reviewable by that tenant's auditor.

A retention policy is **not** fixed in this ADR; institutional
expectation is "as long as the audit log itself", which for the
financial-services audience is typically 10 years. Implementation
adds a deployment-configurable retention if and when the operator
faces an external retention constraint.

### 5. Bootstrap pathway

The Phase-1 bootstrap workflow `portfoliflow bootstrap` is
extended (per ADR-0063 §6) to:

1. Apply migrations.
2. Seed the system tenant (`SYSTEM_TENANT_ID`, subdomain `admin`,
   name `Platform Administration`) idempotently.
3. Rename the previously-sentinel tenant to "Minathena Capital",
   subdomain `minathena-capital`.
4. Read `SUPER_ADMIN_EMAIL` and `SUPER_ADMIN_PASSWORD` from `.env`
   (or from `--super-admin-email` / stdin). Create or update the
   first super-admin user idempotently. Drift detection on
   `is_super_admin` and `is_active` (same pattern as the existing
   sentinel-user drift check).
5. Read `OWNER_EMAIL` / `OWNER_PASSWORD` for Minathena Capital's
   initial owner. Create idempotently.
6. Continue with existing seed steps (asset classes, sectors,
   regions, ...).

The bootstrap is the standard first-run path on a fresh
deployment. After bootstrap, additional super-admins are created
either via the web UI (`/super-admin/users` route) or via the
standalone CLI `portfoliflow create-super-admin`. The CLI path is
the operational backup if the only super-admin account is locked
out.

### 6. Subdomain-resolver contract for the admin subdomain

The `SubdomainTenantResolver` (ADR-0063 §1) treats `admin` as a
valid tenant subdomain and resolves it to `SYSTEM_TENANT_ID`. No
special-case code path: the system tenant is a row in `tenants`
with `subdomain = 'admin'`, and the resolver finds it via the
same query that finds Minathena Capital.

The login route at `admin.portfoliflow.net/login` is the
*same* `/login` route as on any other subdomain. The difference
is what the user is allowed to do after login. The route does not
check the subdomain; it checks `user.is_super_admin` and
`user.tenant_id == SYSTEM_TENANT_ID` after authentication. A
super-admin who somehow types `minathena-capital.portfoliflow.net/login`
and uses their super-admin credentials gets the generic "Invalid
credentials" response because the resolver resolves the host to
the wrong tenant for their account. (The `users.email` uniqueness
is `(tenant_id, email)`, so a super-admin's email does not collide
with a tenant user's email even if they happen to be the same
string.)

## Rationale

- **No web-side tenant-data access for super-admins** is a
  stronger structural commitment than "tenant-data access is
  audited". The operator's institutional pitch ("I cannot see
  your data from the web UI") becomes structurally true rather
  than policy-true. The CLI backdoor is unbypassable from a
  remote attacker's perspective (requires shell access on the
  server host) and audited every time it is used. The
  combination is the right shape for the target audience.
- **CLI as the emergency pathway**, not as the primary pathway.
  An operator who needs daily tenant-data access does not have
  the right separation of concerns; the design forces tenant-data
  access to be a deliberate, audited, server-side action.
- **`super_admin_audit` as a dedicated table**, not as rows in
  the existing `audit_log`. Super-admin actions span tenant
  boundaries (e.g. "created tenant X"); they do not fit the
  `audit_log` row shape (which assumes a single tenant context).
  Mirroring `inspect-tenant` outputs into the target tenant's
  `audit_log` gives the tenant-side visibility without
  conflating the schemas.
- **Mandatory `--reason` on `inspect-tenant`** prevents the
  command from becoming a silent habit. Forcing the operator to
  write the justification at invocation time creates a usable
  audit trail later.
- **Read-only by integration test**, not by code review alone.
  An ADR-level commitment that "this CLI is read-only" is worth
  more when an automated test continually verifies it. The same
  pattern as the audit-engine-usage regression test from
  ADR-0036 §8.
- **No `run-as-tenant-owner` subcommand.** The mere existence of
  such a command would undermine the structural guarantee. The
  recovery scenario it would address is rare enough (operator
  must modify a tenant's data without the tenant's owner being
  reachable) that the right response is a custom one-off
  intervention with explicit consent, not a documented routine
  command.
- **Re-using `LocalPasswordAuthBackend` instead of a separate
  super-admin auth backend.** The credential-verification logic
  is the same; what differs is the post-authentication
  authorisation. Centralising the verification keeps the
  constant-time discipline, the lockout policy, and the
  audit-write path in one place.

## Alternatives Considered

- **Web-side impersonation (N3-A from the design discussion).**
  A "view as tenant X" button in the super-admin UI that
  switches the request's `app.tenant_id` to the target tenant
  and lets the super-admin see tenant data with a prominent
  impersonation banner. Rejected: the structural guarantee
  ("super-admin cannot see tenant data from the web UI") is
  stronger than any policy or banner. Institutional audiences
  appreciate the structural variant. The pattern can be added
  later if real customer needs surface; the schema does not
  preclude it (the audit infrastructure would in fact be reused
  unchanged).
- **Aggregated/anonymised tenant-data view for super-admins
  (N3-B).** The super-admin sees synthetic summary statistics
  per tenant — "47 investments, last upload 3 days ago, 2 active
  users" — but no row-level data. Rejected as the primary
  surface: even aggregate counts ("number of investments") are
  business-data signals that a customer might prefer the
  platform operator not have visibility into. The minimal
  surface ("tenant exists, user count, activation state") is
  the strict variant.
- **Encrypted tenant data with platform-blind keys (N3-C).**
  Column-level encryption with per-tenant keys held only by the
  tenant owner. Genuinely platform-blind. Rejected as
  over-engineering for the current phase: the engineering cost
  is multiple ADRs of work, the operational cost is recovery
  scenarios where a tenant loses their key, and the regulatory
  requirements at the Phase-1 deployment scale do not require
  it. Re-considered if and when a regulated customer asks
  explicitly.
- **Super-admin as a column on `users` without a separate
  system tenant.** Already rejected in ADR-0063 §2 for the
  reasons covered there.
- **CLI-only platform operations (no admin web surface at all,
  D3 option SA-B).** Rejected: tenant management (creation,
  deactivation, owner reset) is a routine operational task; a
  CLI-only surface scales poorly with multiple operators and
  requires SSH access for every routine action. The web
  surface plus the CLI emergency path is the right division.
- **OIDC for super-admins, password for tenants.** Could be
  added later (super-admin identity is "the operator's
  workforce IdP"); not committed in this ADR. The auth-backend
  abstraction from ADR-0036 §3 allows it additively.

## Consequences

### Positive

- The institutional pitch ("I cannot see your data from the web
  UI") is a true statement, not a policy commitment.
- The CLI emergency path exists, is audited, is read-only by
  test, and is deliberately friction-loaded (server access +
  reason argument).
- Super-admins operate against a clearly-bounded surface; the
  blast radius of a super-admin credential compromise is
  "tenant management can be misused" rather than "tenant data
  can be exfiltrated". (MFA on super-admin accounts closes most
  of the remaining gap; see Follow-ups.)
- The schema invariant (`is_super_admin = TRUE` only when
  `tenant_id = SYSTEM_TENANT_ID`) is enforced at the database
  level; the route-level surface is the second defence layer.
- `super_admin_audit` plus the mirror writes into target
  tenants' `audit_log` give both the operator's own compliance
  review and the tenant's auditor a complete view of the
  super-admin's interactions with the tenant.

### Negative

- The CLI emergency path's friction is also its cost: in a
  genuine incident at 03:00, the operator needs server shell
  access (which is the right requirement for an emergency
  pathway, but is a higher operational bar than a web UI).
- No "view as tenant" capability means support for customers
  who need help interpreting their own data is constrained:
  the customer must reproduce the issue with their owner
  account active and screen-share, or the operator coordinates
  out-of-band. For the Minathena Capital deployment scale
  (a small number of customers, all known by name), this is
  acceptable. At larger scale it may become an operational
  pain point — re-evaluate.
- The `inspect-tenant` command requires server shell access,
  which means a deployment topology with no admin shell
  (e.g. a managed Kubernetes pod with no exec access) cannot
  use it. Document the requirement in the deployment guide.
- `super_admin_audit` adds another append-only table that grows
  unbounded; retention is a Phase-5+ concern.
- MFA absence on super-admin accounts has higher impact than on
  tenant accounts. The C0 Hetzner-Hardening roadmap item lists
  it explicitly.

### Neutral / Follow-ups

- **MFA on super-admin accounts.** Higher priority than tenant
  MFA; introduced as a sub-item of the global MFA work
  (post-MVP) but with super-admin coverage prioritised.
- **OIDC for super-admins.** Future ADR; trivial alongside the
  auth-backend abstraction.
- **Tenant deletion (hard delete).** Distinct from deactivation;
  involves DSGVO Art. 17 (right to erasure) and full data
  removal. Out of scope here; the schema and audit surface
  support it when introduced.
- **Per-tenant retention policy** for `audit_log` and the mirror
  writes from `inspect-tenant`. Out of scope here.
- **Super-admin "session diagnostics" page** that lets a
  super-admin see *their own* active sessions and revoke them.
  Useful self-service; not in this ADR's scope.
- **`view-as-tenant`-style impersonation, if customer need
  surfaces.** Not foreclosed; would be a separate ADR.
- **Compliance-grade "operator's compliance report"** that
  surfaces `super_admin_audit` content in a tenant-by-tenant
  view for the platform operator's own audit cycles. Out of
  scope here.

## Implementation Notes

- **Affected files (new):**
  - `web/routes/super_admin.py` — the `/super-admin/*` route
    surface.
  - `services/super_admin/__init__.py` — super-admin service
    layer (tenant creation, owner reset, super-admin user CRUD).
  - `cli/inspect_tenant.py` — the read-only emergency CLI.
  - `cli/create_tenant.py`, `cli/create_super_admin.py`,
    `cli/create_user.py` — standalone platform-operations CLIs.
  - Migration `b013_super_admin_audit.py` — adds the
    `super_admin_audit` table.
  - Regression tests
    (`tests/regression/test_super_admin_routes_no_tenant_data.py`,
    `tests/cli/test_inspect_tenant_is_read_only.py`).

- **Affected files (modified):**
  - `cli/bootstrap.py` — extended with super-admin and
    target-tenant seeding (per §5 above).
  - `cli/__init__.py` — register the new typer subcommands.
  - `web/main.py` — wire the super-admin router; no
    cross-cutting changes (the router self-registers).
  - `web/auth.py` — `require_super_admin` dependency wired
    against the new `is_super_admin` column.
  - `services/auth/local_password.py` — accepts
    `SYSTEM_TENANT_ID` as a valid `tenant_id` argument from
    the admin login route.
  - `tests/regression/test_audit_engine_usage.py` — extended
    sanctioned-usage list to include `super_admin` reads on
    `tenants` and writes on `super_admin_audit`.

- **Removed surfaces:** none. This ADR is additive on top of
  ADR-0063.

- **Documentation:**
  - `CLAUDE.md` glossary entries added: **Super-admin**,
    **System tenant**, **Platform operations**.
  - Deployment guide section: super-admin bootstrap procedure
    (initial credentials via `.env`, first-login workflow,
    rotation of the initial credentials).
  - Operations runbook: `inspect-tenant` usage policy
    ("only with documented tenant consent, captured in the
    `--reason` argument").

## Compliance & Audit Relevance

- **BAIT AT 9 (Outsourcing) and VAIT Chapter 9.** The platform-
  operator-as-processor relationship to the tenant-as-controller
  benefits from the structural data-access constraint: the
  processor's web surface cannot read controller-side data, and
  the controller's auditor can verify this via the
  forbidden-tables regression test and the
  `super_admin_audit` / mirror-`audit_log` records.
- **DSGVO Art. 28 (Processor obligations) / Art. 32 (Security
  of Processing).** The "platform operator cannot read tenant
  data from the web" structural commitment is the kind of
  documented technical-organisational measure (TOM) a DSGVO
  audit looks for. The CLI emergency path's `--reason`
  argument plus the mirror write into the target tenant's
  `audit_log` give the controller-side visibility the regulation
  expects.
- **MaRisk AT 7.2.** The Mandantentrennung extends to the
  platform-operations role: a super-admin is *structurally*
  not part of any tenant.
- **ISO 25010.** Confidentiality (tenant data not exposed
  through any super-admin route), Accountability
  (`super_admin_audit` + mirror writes), Security
  (defence-in-depth: schema CHECK + route guards + read-only
  CLI by integration test), Maintainability (no separate
  super-admin auth backend; the platform-operations CLIs follow
  the existing typer pattern).
- **DORA (Operational Resilience).** Documented, tested
  emergency procedures (the `inspect-tenant` pathway) with an
  audit trail.
- **Audit evidence.**
  - Migration `b013_super_admin_audit.py`.
  - `pg_policies` for `super_admin_audit` (read-only for
    super-admins; writes via audit engine).
  - The forbidden-tenant-data-table regression test
    (`test_super_admin_routes_no_tenant_data.py`).
  - The read-only CLI integration test
    (`test_inspect_tenant_is_read_only.py`).
  - `super_admin_audit` table contents over time.
  - The mirror rows in target tenants' `audit_log` corresponding
    to each `inspect-tenant` invocation.
  - The deployment guide section codifying the super-admin
    bootstrap and the `inspect-tenant` usage policy.

## References

- ADR-0035 (Multi-Tenant Architecture: Tenant Isolation via RLS)
  — the substrate the super-admin route surface deliberately
  does **not** consume from.
- ADR-0036 (Authentication Strategy) — the auth backend the
  admin login route shares with tenant logins.
- ADR-0040 (Sentinel Bootstrap CLI-driven) — the precedent
  extended here to multi-tenant.
- ADR-0063 (Multi-Tenant Activation Phase 1) — the companion
  ADR introducing the `is_super_admin` axis and the system
  tenant.
- DSGVO Art. 28, Art. 32 — Processor obligations and Security
  of Processing.
- BAIT AT 9 — Outsourcing controls.

---

## Revision History

| Date       | Author                       | Change                                                                                                                                                                                                          |
|------------|------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 2026-05-26 | PortfoliFLOW project owner   | Initial draft. Records the no-tenant-data-from-web structural commitment, the CLI emergency pathway (read-only, `--reason`-mandatory, regression-test-enforced), the `super_admin_audit` table + target-tenant `audit_log` mirroring, and the bootstrap path. |
| 2026-06-03 | PortfoliFLOW project owner   | Status corrected to **Accepted** to match shipped code during doc/code reconciliation: migrations `b013_super_admin_audit` / `b014_super_admin_audit_nullable_actor`, `services/super_admin/operations.py`, the `/super-admin/*` routes, and the CLI subcommands (`create-super-admin`, `inspect-tenant`). |
