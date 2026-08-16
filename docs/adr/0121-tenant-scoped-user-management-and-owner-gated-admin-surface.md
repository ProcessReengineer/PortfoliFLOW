# ADR-0121 — Tenant-Scoped User Management with Owner-Gated Admin Surface

- **Status:** Accepted (2026-08-14)
- **Date:** 2026-08-14
- **Tags:** users, roles, permissions, admin, multi-tenant, rls, audit, sessions, release
- **Related:** ADR-0063 (multi-tenant activation, role substrate), ADR-0064 (super-admin
  surface and CLI), ADR-0065 (transaction-lifetime discipline), ADR-0078 (app-role
  switch inside `tenant_context`), ADR-0112 §6 (Providers & Credentials surface —
  the render-path idiom this ADR reuses)
- **Roadmap:** #015 sub-item **B1f** (Tenant-owner user management). No new roadmap
  ID is issued; the strand executes B1f. Implementation strands: **U1** (service),
  **U2** (web surface). **U3** (super-admin per-tenant user view) is deferred past
  the AGPL release — see §7.

## Context

PortfoliFLOW ships its AGPL public release with a user-management story that is
complete at the platform level but absent at the tenant level. A super-admin can
create a tenant together with its first owner — atomically, with an audit row —
through both the super-admin UI and the CLI (`create_tenant_idempotent`, ADR-0064).
Additional tenant users can only be created through the CLI
(`portfoliflow create-user`), which runs on the superuser engine and requires
`SUPER_ADMIN_EMAIL` for actor attribution. A tenant owner has no in-product way to
list, create, deactivate, or reset the password of the users of their own tenant.

The substrate this ADR builds on already exists, verified against the 2026-08-14
snapshot:

1. **Role model.** Migration **b012** (`multi_tenant_activation`) replaced
   `users.is_tenant_owner` with `users.roles TEXT[]`, CHECK-constrained to
   `{'owner','member','auditor'}` (mirrored by `ALLOWED_ROLES` in
   `core/repositories/user_repository.py`). `auditor` is declared but gates
   nothing anywhere in the codebase today.
2. **Role gating in the web layer.** `web/permissions.py` provides
   `get_authenticated_user`, the `require_role(*roles)` dependency factory
   (plain **403**, `"insufficient role"`, no redirect), and
   `require_super_admin`. `require_role("owner")` is already in production use —
   notably on the tenant panel of Providers & Credentials
   (`web/routes/provider_credentials.py`, ADR-0112 §6), where the POST route is
   gated by the dependency and `_render_section` fetches and renders tenant rows
   only for owners. The route is authoritative; the template mirrors it. **The
   owner/member seam through Providers & Credentials that this strand was
   originally chartered to build already exists and is not touched.**
3. **RLS and audit.** `users` is under `apply_tenant_rls('users')`;
   `users_audit_trigger` writes to `audit_log` and captures the actor from the
   `app.user_id` GUC, which `tenant_context(engine, tenant_id, user_id=…)` sets.
   Writes through the app role inside `tenant_context` are therefore
   tenant-confined structurally and audited with actor attribution at no extra
   application cost.
4. **Email uniqueness is per-tenant.** `uq_users_tenant_email` on
   `(tenant_id, email)` from the initial schema. The same email may exist in two
   tenants; within one tenant it may not. This ADR documents the constraint and
   builds on it; it does not redesign it.
5. **Password policy exists.** `services/auth/password_policy.py` →
   `validate_password_strength` (minimum 12 characters, at least 2 character
   classes; set-time only, never verify-time). Today it is enforced by the CLI
   `set-password` path but **not** by `create_user_idempotent` (super-admin path
   and `create-user` CLI).
6. **Session invalidation exists as an app-level method.**
   `SessionRepository.delete_all_for_user` (`services/auth/session.py`); the
   bootstrap `set-password` establishes the pattern of rotating the hash and
   deleting all sessions in one transaction (per OWASP session-management
   guidance).
7. **Shared validation lives in the super-admin module.** `_validate_email` and
   `_validate_roles` are private helpers of `services/super_admin/operations.py`,
   alongside the `CannotDeactivateLastSuperAdminError` guard pattern this ADR
   mirrors at tenant scope.
8. **The Admin area is a stack of sections, not tabs.**
   `web/templates/_partials/areas/_admin_body.html` composes `data-import`,
   `market-data`, `providers-credentials`, and an `application-settings`
   placeholder through the `areas/_section.html` idiom, with lazy-loaded section
   bodies. The area subtitle already promises "user administration".

No schema migration is required: `users.roles`, `users.is_active`,
`users.display_name`, and `users.password_hash` all exist. `UserRepository`
already provides `list_all`, `create`, `get_by_id`, `get_by_email`, and
`set_password_hash`; only activation and role mutation are missing.

The hard constraint is the AGPL release this weekend (roadmap #052): the strand
must land in a shape where a partial landing still leaves a releasable state.

## Decision

### §1 — Tenant-scoped user service under the app role

A new service package **`services/tenant_users/`** implements the tenant-side
user operations: **list, create, deactivate, reactivate, reset password, change
role (owner↔member)**. Every operation runs under the application role inside
`tenant_context(engine, tenant_id, user_id=actor_user_id)`:

- **RLS confines reach structurally** — the service cannot touch another
  tenant's rows even if handed a foreign user id.
- **The audit trigger attributes the actor** — the `app.user_id` GUC carries the
  acting owner; no service-level audit code is written.

The service does **not** use the superuser engine and does **not** write
`super_admin_audit`. The super-admin path (ADR-0064) is unchanged and remains
the only user of the superuser-engine operations.

### §2 — Shared validation is extracted, not duplicated

`_validate_email` and `_validate_roles` move from
`services/super_admin/operations.py` into a shared module (location decided at
implementation time within `services/`; the super-admin module re-imports them).
Behaviour is identical; `services/super_admin/operations.py` keeps its public
surface. Extraction is a pure refactor covered by the existing super-admin
tests.

### §3 — Password mechanics

- The owner sets initial passwords at creation and performs resets directly.
  There is **no** "must change at first login" flag (would require a migration).
- Both create and reset in `services/tenant_users/` enforce the existing
  `validate_password_strength`. No new policy is defined; one policy, one
  source.
- **Known asymmetry, accepted:** `create_user_idempotent` (super-admin UI + CLI)
  does not enforce the policy today and is **not** changed by this ADR — a
  behaviour change on a working operator path immediately before the release is
  the wrong trade. Alignment is a follow-up noted on B1f.
- Member self-service password change is out of scope. In v1, password change
  always runs through an owner (or, for edge cases, operator CLI paths).

### §4 — Guards

A single guard helper enforces, inside the same transaction as the write:

1. **Last-active-owner protection.** The last active user holding `owner` in a
   tenant can neither be **deactivated** nor **demoted** to `member`. Counting:
   active rows with `'owner' = ANY(roles)` in the current tenant, read within
   the writing transaction. Error type `CannotDeactivateLastOwnerError` /
   `CannotDemoteLastOwnerError`, mirroring the
   `CannotDeactivateLastSuperAdminError` pattern.
2. **No self-deactivation**, for any role, even when other owners exist. An
   owner who wants to leave has another owner deactivate them.
3. **Self-demotion is allowed** when at least one other active owner exists —
   the legitimate hand-over case. The running session stays valid; the next
   owner-gated request answers 403.
4. **Owners are peers.** An owner may deactivate or demote another owner,
   subject only to guard 1. No hierarchy among owners is introduced.
5. **Session invalidation** via `SessionRepository.delete_all_for_user` on
   **deactivation** and on **password reset**, in the same transaction as the
   write (the bootstrap `set-password` precedent).

### §5 — Repository additions

`UserRepository` gains two methods, following the existing method style and
returning DTOs: **`set_active(user_id, active)`** and
**`set_roles(user_id, roles)`** (validated against `ALLOWED_ROLES`). No other
repository changes.

### §6 — Web surface: Users section in the tenant Admin area

A new **Users** section is added to `_admin_body.html` through the established
`areas/_section.html` idiom with a lazy-loaded section body — a sibling of
Providers & Credentials, not a new navigation concept.

- **Routes** live in a new `web/routes/tenant_users.py`. Every route — including
  the section GET — is gated by **`Depends(require_role("owner"))`**; mutating
  routes additionally take `verify_csrf`. The existing plain-403 semantics of
  `require_role` are adopted unchanged; no redirect variant is introduced. The
  route is authoritative; templates only mirror the gate (members simply never
  receive the section body).
- **Render path** follows the ADR-0112 §6 idiom: one `_render_section` helper
  serves the GET and every POST re-render; state is re-read after each write
  inside one short `tenant_context` (Pattern B, ADR-0065); a rejected write
  answers **400 with the same section body** carrying an inline error banner.
- **Forms:** create (email, display name, initial password, role owner/member),
  deactivate/reactivate, reset password, change role. The role selector offers
  **owner and member only** — `auditor` gates nothing and is not offered
  (service and DB continue to accept it; see §8).
- The section is rendered for owners only; for the Admin area shell this means
  the Users section include is wrapped in the owner conditional the shell can
  derive from `request.state.user` — cosmetic mirroring of the authoritative
  route gate.

### §7 — U3 is deferred past the release

The super-admin per-tenant user view (list a tenant's users, create additional
users from the super-admin surface) is **not** in the release gate. The
super-admin already creates tenant + first owner atomically (UI and CLI), and
from U2 onward that owner manages all further users in-product. U3 is recorded
on roadmap #015/B1f as an open follow-up; the existing superuser-engine
operations remain its designated substrate.

### §8 — Not in scope

- U3 (super-admin per-tenant user management UI) — deferred, §7.
- Password-policy enforcement on `create_user_idempotent` / `create-user` CLI —
  documented asymmetry, follow-up on B1f (§3).
- Member self-service password change; "must change at first login" flag.
- `auditor` in the UI; any semantics for the `auditor` role.
- Owner-gating of other Admin sections (Data Import, Market Data) — a separate,
  later decision.
- Invitation flows, email delivery, self-registration.
- Any change to Providers & Credentials — the owner/member seam there exists
  and is untouched.
- Any schema migration. If implementation discovers one is needed, that is a
  stop-and-report moment, not a silent addition.
- Any change to the super-admin surface, `services/super_admin/operations.py`
  behaviour (beyond the §2 extraction), or the CLI commands.

## Consequences

**Positive.**

- A tenant becomes self-administering the moment its first owner exists; the
  CLI `create-user` path becomes an operator fallback instead of the only path.
- Tenant-side writes inherit tenant confinement and actor-attributed auditing
  from the substrate (RLS + trigger + GUC) rather than from new code — the
  BAIT/VAIT posture improves without new audit machinery.
- The release-critical scope is small: one service package, two repository
  methods, one route module, one section template. U1+U2 land independently;
  a partial landing (U1 only) leaves the codebase releasable since the service
  is inert without routes.
- One validation source (§2) and one password policy (§3) instead of a fork.

**Negative / accepted costs.**

- The password-policy asymmetry (§3) means the super-admin path can still set
  weak passwords until the follow-up lands.
- Plain-403 on owner-gated routes means a member who hand-crafts a URL sees a
  bare error rather than a friendly page — consistent with every existing
  `require_role` surface, revisitable globally later.
- Self-demotion with a still-valid session (§4.3) produces 403s on owner
  surfaces mid-session rather than a forced re-login — deliberate, as the
  session itself remains legitimate.
- `auditor` remains a declared-but-dormant role; the UI narrowing (§6) makes
  the dormancy explicit rather than resolving it.

## Alternatives considered

- **Routing tenant user writes through the superuser engine** (reusing
  `create_user_idempotent` directly): rejected — it would bypass RLS
  confinement, require synthetic super-admin actor attribution for a tenant
  actor, and write platform audit rows for tenant-level events.
- **Section-level template-only gating** for the Users section: rejected — the
  route must be authoritative (established by ADR-0112 §6); templates mirror.
- **Redirect-based `require_owner`** (303 to the Admin area with a flash):
  rejected for v1 — it would fork the established 403 idiom used at ~35 call
  sites for one surface.
- **Blocking the release on U3**: rejected — the tenant-creation path already
  produces the first owner; U3 is convenience, not capability.

## Revision History

| Date | Change |
| --- | --- |
| 2026-08-14 | Initial draft (Proposed). |
| 2026-08-14 | Accepted; registered in the ADR index. |
