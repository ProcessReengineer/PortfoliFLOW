# ADR-0036: Authentication Strategy — Session-Based with OIDC-Readiness

- **Status:** Accepted
- **Date:** 2026-05-03
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, authentication, security, session-management

---

## Context

ADR-0019 deliberately deferred the authentication / authorisation
question on the grounds that PortfoliFLOW was single-user and the
investment in those subsystems would have been premature. ADR-0033
removes that premise: the web variant is multi-user, and at least
Phase-2 demonstration requires that *some* mechanism authenticates
the user before a request reaches Shirley, the DataStore, or any
tool. ADR-0035 puts isolation at the database level — but RLS
needs an authenticated user identity to bind to a tenant. Without
an authentication layer, there is nothing to bind.

This ADR is where ADR-0019's deferred question is finally answered.

The competing forces are recognisable:

- **Institutional expectations.** Versorgungswerke, FoF boutiques,
  and asset managers expect documented authentication, audit-grade
  logging of access events, configurable lockout policies, and
  (for production rollout) MFA. BAIT AT 7.2, VAIT Chapter 7, and
  MaRisk AT 7.2 are explicit about identity and access management.
- **Solo-developer ressources.** A full identity stack —
  registration flows, password reset, MFA enrolment with recovery
  codes, OIDC integration with multiple IdPs, passkey support —
  is months of work. Phase 2 is a soft-pitch demo running against
  a sentinel tenant; it does not need that depth.
- **Strangler discipline (ADR-0039).** The migration must remain
  demo-stable at every phase. An over-engineered Phase-2 auth that
  blocks the demo would defeat the strategy.
- **ADR-0022's "single trusted user" assumption.** The Tool Trust
  Classes and Gating Policy was written for a setting where every
  user is the operator-developer. Multi-user invalidates that
  assumption. This ADR cannot solve the resulting question (which
  trust classes which users may invoke) but must name it.

The decision PortfoliFLOW makes is therefore not "which auth stack
do we ship in Phase 5"; it is "what is the smallest credible
auth surface for Phase 2 that does not paint Phase 5 into a
corner". The answer is local session-based authentication with the
schema and middleware shaped so that OIDC slots in additively.

This decision is squarely security-, audit-, and compliance-
relevant: BAIT, VAIT, MaRisk, DSGVO, ISO 25010 (Security,
Authenticity, Accountability), DORA. The Compliance & Audit
Relevance section of this ADR is correspondingly substantial.

## Decision

### 1. Phase 2: local session-based authentication

Users authenticate by **email + password**. Passwords are stored as
**Argon2id** hashes (recommendation; bcrypt is an admissible
implementation-time fallback if the chosen identity library does
not support Argon2id natively, with the OWASP-recommended cost
parameters).

Sessions are server-side and identified by a session cookie:

- `HttpOnly` — JavaScript cannot read the cookie.
- `Secure` — TLS-only.
- `SameSite=Lax` (or `Strict` if no cross-site flows arise during
  Phase 2).

Session state is persisted in a `sessions` table in Postgres
(no separate Redis dependency in Phase 2). The table is
tenant-scoped per ADR-0035 and follows the audit-field pattern of
ADR-0034.

**Timeouts.** Idle timeout 8 hours; absolute timeout 24 hours.
Both values are tenant-configurable (a single global default in
Phase 2; per-tenant overrides become real in Phase 5).

**CSRF.** All mutating requests carry a CSRF token validated
against a value bound to the session. Implemented via FastAPI
middleware or explicit per-route dependencies. GET requests are
not CSRF-protected; POST/PUT/PATCH/DELETE are.

### 2. User table — central fields

The user table is tenant-scoped (per ADR-0035) and audit-tracked
(per ADR-0034). The relevant authentication-bearing columns:

```
users
  id                UUID PRIMARY KEY
  tenant_id         UUID NOT NULL REFERENCES tenants(id)
  email             TEXT NOT NULL
  password_hash     TEXT                  -- nullable; NULL for OIDC-only users
  external_idp      TEXT                  -- nullable; e.g. 'entra-id', 'authentik'
  external_subject  TEXT                  -- nullable; stable subject claim from IdP
  is_tenant_owner   BOOLEAN NOT NULL DEFAULT FALSE
  is_active         BOOLEAN NOT NULL DEFAULT TRUE
  + audit fields per ADR-0034

  UNIQUE (tenant_id, email)
  UNIQUE (external_idp, external_subject)               -- where non-NULL
  CHECK  (password_hash IS NOT NULL
          OR (external_idp IS NOT NULL
              AND external_subject IS NOT NULL))
```

The `CHECK` constraint guarantees that every active user is
authenticatable through at least one configured backend.

`is_tenant_owner` is the Phase-2 approximation of a role model: a
boolean that says "this user is the privileged user inside this
tenant". A full role model is a Phase-5 concern.

### 3. Auth backend abstraction

Authentication is mediated by an abstract `AuthBackend` interface
exposing minimum methods such as
`authenticate(credentials) -> User | None` and
`create_session(user) -> Session`. The login endpoint and the
session middleware speak only to this interface; the choice of
backend is configurable per tenant.

- **Phase 2.** One implementation: `LocalPasswordAuthBackend`.
- **Phase 5.** A second implementation:
  `OIDCAuthBackend`. Tenant configuration selects which backend
  is active. The login UI dynamically renders a password form or
  an "Sign in with ..." button based on the tenant's configured
  backend.

The abstraction is the architectural reason this ADR's
"OIDC-Readiness" qualifier is not aspirational: adding OIDC in
Phase 5 is a new implementation alongside the existing one, not a
refactor of authentication code.

### 4. MFA: not in Phase 2; mandatory before institutional rollout

Multi-factor authentication is **not** implemented in Phase 2.
The user table and session schema do not contain MFA-specific
columns yet; those are added in the Alembic migration that
introduces MFA, which lands before the first institutional
production rollout.

The Phase-2 omission is deliberate, not an oversight:

- TOTP enrolment with recovery codes, the realistic minimum, is a
  non-trivial UX surface that has no demo value at the soft-pitch
  stage.
- The Phase-2 user is the operator (sentinel user); the device
  they use already has the full secrets in `.env`.
- Pre-emptively shipping MFA would force the developer to design
  the recovery-code flow before the rest of the auth surface
  stabilises.

The follow-up is named explicitly: **before any institutional
production rollout, MFA must be implemented.** TOTP via
`pyotp` (or equivalent) with recovery codes is the expected
minimum; WebAuthn-as-second-factor is a credible upgrade beyond
that. A Phase-5 ADR will record the chosen mechanism.

### 5. Passkeys / WebAuthn: future, not decided here

Passkeys (FIDO2 / WebAuthn as the primary credential, replacing
or complementing the password) are noted as a **future** auth
variant alongside local + OIDC. The decision belongs to a separate
ADR after Phase 5 stabilises, when there is an actual user base to
roll the feature out to. No schema changes are pre-emptively made.

### 6. Sentinel user in Phase 2

On bootstrap, the sentinel tenant of ADR-0035 is paired with a
sentinel user provisioned from environment variables:

- `SENTINEL_EMAIL` — the email address.
- `SENTINEL_PASSWORD` — the initial password (Argon2id-hashed
  before storage; plaintext is never persisted).

Both variables are required when starting Phase 2 deployments;
their absence is a fail-loud bootstrap error, not a silent default.

The sentinel user has `is_tenant_owner = TRUE` **inside the
sentinel tenant**. They have **no cross-tenant rights**. They are
not the Postgres superuser, not the database owner, not the OS
service account — three distinct identities — and the application
explicitly refuses to grant them implicit access to other tenants
even when only the sentinel tenant exists.

The sentinel user can change their password through the standard
UI flow once authenticated. The `.env` value is the bootstrap
seed, not a perpetual credential.

### 7. Tool-trust classes under multi-user — explicitly out of scope

ADR-0022's gating policy is built around a single trusted operator.
Multi-user invalidates that premise: a viewer-role user must not
be able to invoke `WRITE_INTERNAL` or `EXTERNAL_EFFECT` tools, and
a tenant-owner role probably should be allowed all of them. The
mapping from roles to tool-class permissions is a real design
question that this ADR cannot answer — it requires the role model
to exist first.

This ADR's pragmatic Phase-2 stance: the sentinel user, being the
sole Phase-2 user and being the tenant owner, is permitted to
invoke every tool class. The behaviour matches ADR-0022's
single-user assumption while it is still factually true. The
follow-up is named:

> A separate ADR will define the per-role overlay on tool-trust
> classes, written before Phase 5's multi-user activation. Until
> then, ADR-0022's gating policy operates as documented.

### 8. Login security minima for Phase 2

- **Account lockout.** N failed login attempts within a window
  triggers a temporary lockout. Default: 5 in 15 minutes.
  Configurable per tenant from Phase 5; a single global default
  in Phase 2.
- **Password complexity.** Minimum length 12 characters. No
  character-class compulsion (NIST SP 800-63B explicitly advises
  *against* mandatory special characters; long passphrases are
  preferred). Common-password rejection (against a small built-in
  blocklist; pwned-password integration optional).
- **Login audit.** Every login attempt — successful or failed —
  is recorded in a `login_audit` table (separate from the
  domain `audit_log` defined in ADR-0035) with timestamp, IP
  address, user agent, success flag, and (on failure) a
  classified reason code. Tenant-scoped, RLS-protected, retained
  per the regulated-industry expectation that login records
  remain available for audit for years.
- **Password reset.** Single-use, time-limited token sent to the
  user's registered email. No security questions. Rate-limited.
  In Phase 2, password reset can be deferred (sentinel user
  resets via `.env` rotation), but the flow is implemented before
  Phase 5 activation.

## Rationale

- **Local password auth in Phase 2 over direct OIDC.** Correctly
  implementing OIDC (token refresh, discovery caching, SLO,
  configuration of multiple IdPs) is a substantial subsystem with
  no demo value at the soft-pitch stage. Local auth is well-
  understood and ships in days. The structural preparation for
  OIDC (the auth-backend abstraction and the user-table columns)
  costs little and preserves Phase-5 optionality cleanly.
- **Argon2id over bcrypt.** OWASP's current recommendation for new
  implementations. Better resistance to GPU-accelerated attacks
  than bcrypt at comparable cost settings. Mature Python
  implementation in `argon2-cffi`.
- **Postgres-backed sessions over Redis.** Adding Redis to the
  Phase-2 stack is operational overhead for a workload that
  Postgres handles trivially at expected scale. Redis becomes
  attractive at the SaaS-scale point — at which time it is a
  configuration change, not a code refactor (the abstraction is
  in the auth backend, not in the storage choice).
- **Auth-backend abstraction.** The most certain prediction about
  the multi-user roadmap is that auth providers will multiply:
  local in Phase 2; OIDC for tenants who run their own IdP in
  Phase 5; passkeys later. An interface today is far cheaper than
  a refactor when the second backend lands.
- **NIST-aligned password rules over legacy complexity rules.**
  Empirical evidence shows that mandatory character-class rules
  drive users to predictable patterns (`Password1!`). Length is
  the dominant signal. NIST SP 800-63B reflects this; PortfoliFLOW
  follows it.
- **Sentinel user, deliberately unprivileged across tenants.**
  Cross-tenant superuser access would be a permanent backdoor in a
  multi-tenant system. Even the sentinel user — sole Phase-2
  occupant — is bound to its tenant by the same RLS rules that
  protect every other tenant in Phase 5.
- **Tool-trust mapping deferred.** Defining role → tool-class
  permissions before the role model exists would be speculation.
  The pragmatic Phase-2 stance keeps ADR-0022's behaviour
  unchanged while the question is left open with an explicit
  follow-up commitment.

## Alternatives Considered

- **Direct OIDC in Phase 2.** Rejected. Implementation cost out
  of proportion to demo value. Demo deployments would need a
  running IdP (Authentik / Keycloak in a container, or a tenant
  in Entra ID), turning a soft-pitch demo into a live
  infrastructure exercise.
- **Passkeys / WebAuthn as the primary credential.** Considered.
  Phishing-resistance and modernity are real benefits. Rejected
  for now: the recovery story is non-trivial, institutional
  adoption in 2026 is uneven, and Phase 2 has no users beyond the
  operator.
- **Magic-link / passwordless login.** Rejected. Requires
  reliable email delivery in the loop of every login attempt and
  is more phishing-vulnerable than commonly believed (users click
  links).
- **JWT with stateless sessions.** Considered. Easier horizontal
  scaling, but JWT revocation is a known weak point — either you
  accept that revocation is impossible, or you keep server-side
  state and lose the stateless property. Cookie-backed sessions
  with server-side state are the simpler and more secure choice
  for a browser application.
- **Full RBAC in Phase 2.** Rejected. With a single user, an RBAC
  model is speculation. The Phase-2 approximation
  (`is_tenant_owner` boolean) covers the only role that exists in
  Phase 2; the full model lands in a Phase-5 ADR alongside the
  tool-trust overlay.
- **Skip MFA permanently / Optional MFA forever.** Rejected.
  Institutional rollout will require it; deferring it past
  Phase 5 would be irresponsible. The deferral is bounded
  ("before institutional rollout"), not open-ended.
- **Use an off-the-shelf identity provider (Auth0, Clerk).**
  Considered. Faster to ship, but: vendor lock-in, data-residency
  questions for institutional customers (where does the user
  database live?), and cost at scale. The local-then-OIDC path
  preserves more optionality.

## Consequences

### Positive

- Phase 2 is demo-stable with a credible authentication surface.
- The auth-backend abstraction makes OIDC a Phase-5 addition
  rather than a refactor; the same applies to passkeys later.
- Multi-tenant user identity is structurally available from day
  one; ADR-0035's RLS context is correctly populated from the
  authenticated session.
- Audit-grade login records are present from Phase 2, which is
  what BAIT/VAIT/MaRisk reviewers will expect to see at any
  institutional review point.
- The deliberate non-decisions (MFA mechanism, role model,
  tool-trust overlay) are named, not silently assumed.

### Negative

- Local password management adds non-trivial implementation
  surface in Phase 2 (registration flow for additional users,
  reset flow, lockout, audit). Phase 2 minimises the visible
  surface (sentinel user only) but the underlying code paths are
  full implementations.
- MFA absence is a real risk if a Phase-2 instance is exposed to
  the open internet. The Phase-2 deployment shape (operator's own
  machine or a private network) is the mitigation; this ADR
  flags the limit clearly rather than papering over it.
- ADR-0022's "single trusted user" assumption persists for
  Phase 2 in tooling terms. The structural overhaul depends on
  the role model that a future ADR will introduce.
- Postgres session storage is fine at Phase-2 scale but adds DB
  load that, at SaaS scale, will eventually motivate a move to
  Redis or to a stateless session model. The cost of the future
  swap is bounded by the auth-backend abstraction.

### Neutral / Follow-ups

- **Mail delivery.** The password-reset flow needs reliable
  email delivery. Phase 2 can run without it (sentinel user
  rotates `.env`); Phase 5 cannot. Mail-provider choice is a
  deployment-time decision.
- **Tool-trust per role.** Named here as a follow-up ADR before
  Phase 5 multi-user activation.
- **MFA mechanism.** Named here as a follow-up ADR before
  institutional rollout; expected minimum is TOTP with recovery
  codes.
- **DSGVO right to erasure.** Hard-deleting users would break
  audit references. The standard pattern is anonymisation
  (replace email with a tombstone, set `is_active = FALSE`) plus
  retention of the audit trail; the precise approach is a Phase-5
  detail.
- **Passkeys.** Future ADR after Phase 5 stabilises.

## Implementation Notes

- **Library choices (recommended; finalised in Phase 1
  implementation).**
  - Password hashing: `argon2-cffi`.
  - Session management: bespoke implementation on top of FastAPI's
    middleware, **or** `fastapi-users` if its defaults align (the
    library brings a lot of standard surface; lock-in risk to be
    weighed at implementation time).
  - CSRF: `starlette-csrf` or a small in-house dependency.
  - OIDC (Phase 5): `authlib`.
- **Bootstrap.** An Alembic seed migration (or a dedicated CLI
  command, e.g. `portfoliflow bootstrap`) creates the sentinel
  tenant and sentinel user from `SENTINEL_EMAIL` / `SENTINEL_PASSWORD`
  if they do not yet exist. Idempotent.
- **`.env` requirements for Phase 2:** `SENTINEL_EMAIL`,
  `SENTINEL_PASSWORD`, `SESSION_SECRET_KEY`, `DATABASE_URL`. Any
  missing value is a hard bootstrap error.
- **Test strategy.** Auth tests run against a dedicated test
  tenant (not the sentinel tenant) and a test user. Session
  cookies are constructed programmatically for repository-level
  tests; a small smoke suite drives the full login flow through
  the FastAPI TestClient.
- **`is_tenant_owner` migration path.** The Phase-2 boolean is
  retained when the role model lands; tenant owners are mapped to
  the corresponding role in the new model so existing accounts do
  not lose privileges.

## Compliance & Audit Relevance

- **BAIT AT 7.2 / VAIT Chapter 7 — Identity and Access
  Management.** Authentication with documented procedures, audited
  login attempts, account lockout, and tenant-scoped roles satisfy
  the BAIT/VAIT minima for Phase-2 operation. The MFA gap is named
  explicitly as a Phase-5 prerequisite and is the dominant
  qualification of this ADR's BAIT/VAIT alignment.
- **MaRisk AT 7.2 — Identitäts- und Berechtigungsmanagement.**
  Phase-2 implementation is approximated through the
  `is_tenant_owner` flag; the full role model is a Phase-5
  follow-up under its own ADR. The current state is documented,
  not concealed.
- **DSGVO Art. 25 (Privacy by Design) and Art. 32 (Security of
  Processing).** Personal data in the user table (email) is
  protected at rest via Postgres-level encryption (deployment-
  configured) and in transit via TLS. Login audit records are
  themselves personal data and are tenant-scoped, retention-
  limited, and access-controlled per ADR-0035's RLS model.
- **DSGVO Art. 17 (right to erasure).** Anonymisation pattern,
  not hard delete; the precise workflow is a Phase-5 ADR.
- **DSGVO Art. 32 password storage.** Argon2id at OWASP-
  recommended parameters is the "appropriate technical measure"
  the article requires for credentials.
- **GoBD.** Login audit records, kept for the regulated retention
  period, contribute to the unalterable accountability trail
  GoBD-relevant business processes expect.
- **DORA (Operational Resilience).** Identity management is part
  of the operational-resilience surface; account lockout, audit
  logging, and the planned MFA addition are the relevant controls.
- **ISO 25010 quality attributes.** Security (Authenticity,
  Accountability, Confidentiality), Maintainability (auth-backend
  abstraction), Portability (no IdP lock-in).
- **Tool-trust classes (ADR-0022).** Single-trusted-user
  assumption explicitly named as broken under multi-user; follow-up
  ADR commitment recorded.
- **Audit evidence.**
  - The Alembic migration that introduces the user / session /
    audit tables.
  - `login_audit` table contents.
  - The auth-backend abstraction in code (the file containing
    the `AuthBackend` interface and the Phase-2 implementation).
  - The bootstrap script's idempotency tests.
  - The Phase-5 follow-up ADRs (MFA, role model, tool-trust
    overlay) when they land.

## References

- ADR-0019 (Planned Multi-User Readiness) — the deferred
  question this ADR answers.
- ADR-0022 (Tool Trust Classes and Gating Policy) — the
  "single trusted user" assumption made structurally invalid by
  multi-user; named as the next ADR's subject.
- ADR-0033 (Web Migration: Architectural Shift) — frame.
- ADR-0034 (Persistence Backend: Postgres) — the substrate the
  user, session, and audit tables live in.
- ADR-0035 (Multi-Tenant Architecture) — tenant model the user
  table inherits.
- OWASP ASVS 4.0 — referenced for session, password, and CSRF
  minima.
- NIST SP 800-63B — referenced for password rules.

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-05-03 | PortfoliFLOW project owner   | Initial draft. Records the Phase-2 authentication strategy (local sessions over Postgres-stored state, Argon2id passwords, login audit, account lockout), the auth-backend abstraction that makes OIDC additive in Phase 5, the deliberate Phase-2 omissions (MFA, full role model, tool-trust per role), and the sentinel-user bootstrap pattern. |
| 2026-05-04 | PortfoliFLOW project owner   | Status moved to **Accepted**. Sub-Strang 2b landed the full Phase-2 auth surface: migration `b003_add_auth_columns_and_tables` adds `password_hash` / `is_tenant_owner` / `is_active` to `users` (with the §2 CHECK constraint and a partial unique index on the OIDC subject pair), creates `sessions` and `login_audit` with RLS enabled — `sessions` via the standard `apply_tenant_rls` helper, `login_audit` via a custom policy that hides NULL-tenant rows from tenant-scoped reads but permits NULL-tenant inserts (so unrecognised-email attempts are still recorded). `tenant_context()` now accepts an optional `user_id` and sets `app.user_id`, so the b001 audit trigger captures the actor for authenticated writes. The `services/auth/` package introduces the `AuthBackend` interface and `LocalPasswordAuthBackend` with constant-time discipline (dummy Argon2id verify on user-not-found), 5/15-minute account lockout, and a separate superuser engine reserved for `login_audit` writes (the asymmetry is asserted by `tests/regression/test_audit_engine_only_writes_login_audit.py`). The `SessionRepository` enforces 8-hour idle and 24-hour absolute timeouts; `web/auth.py` wires the cookie / CSRF / require-session dependency chain (303 redirect for browsers, 401 + `HX-Redirect` for HTMX); `web/routes/login.py` provides `GET/POST /login`, `POST /logout`, and `GET /` as a placeholder protected page. The `portfoliflow bootstrap` CLI now persists the password hash on user creation and detects `is_active` / `is_tenant_owner` drift; `portfoliflow set-password` rotates the hash and invalidates every active session for the user (per OWASP guidance). The deferred items from §8 (MFA, real password reset, tenant-resolution beyond the sentinel hook, per-role tool-trust overlay) remain explicitly out of scope until their own ADRs land. Decider: PortfoliFLOW project owner. |
