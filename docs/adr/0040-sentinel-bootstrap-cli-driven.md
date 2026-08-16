# ADR-0040: Sentinel Bootstrap — CLI-Driven Idempotent Initialization

- **Status:** Accepted
- **Date:** 2026-05-04
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, bootstrap, cli, deployment, multi-tenant

---

## Context

ADR-0035 §8 and ADR-0036 §6 jointly specify the existence of a sentinel
tenant and a sentinel user as the structurally-anchored single tenant /
single user that activates Phase 2 of the web migration. Phase 1 created
the schema substrate (`tenants`, `users`, audit-trigger) and reserved
`SENTINEL_TENANT_ID` in `core/tenant_constants.py`, but inserted no
data — the bootstrap mechanism itself was deferred to Phase 2.

Phase 2 must now decide *how* the sentinel rows are inserted. Three
candidates were on the table:

- **Alembic seed migration.** A migration `b003_seed_sentinel` runs
  alongside schema migrations, with `INSERT ... ON CONFLICT DO NOTHING`
  for idempotency.
- **Dedicated CLI command.** A `portfoliflow bootstrap` subcommand,
  idempotent, reads `.env` for sentinel credentials, can be re-run
  without side effects.
- **FastAPI startup hook.** A `lifespan`-context handler runs the
  bootstrap on each application start.

The constraints driving the decision:

- **Schema migrations should be schema-invariant.** Sentinel email,
  password hash, and (later) per-tenant override values are
  environment-specific. Embedding them in an Alembic migration mixes
  concerns and complicates customer-specific deployments (a
  Minathena Capital pilot deployment will plausibly want
  `admin@minathena-capital.com` and not the development default).
- **Phase 5 multi-tenant deployments.** A future `create-tenant` /
  `create-user` workflow follows the same pattern as bootstrap. A CLI
  is the natural extension point; an Alembic-only path would force
  parallel implementation later.
- **Operations clarity.** Bootstrapping data is a deployment concern,
  not an application-startup concern. Multi-worker FastAPI deployments
  (Phase 5 will ship `gunicorn -w N`) make a startup hook racy without
  added locking. A CI / deployment pipeline that wants to verify the
  schema without inserting data has no clean path under a startup-hook
  approach.
- **Idempotency honesty.** A CLI can implement read-then-write
  idempotency that explicitly logs what it changed; an Alembic
  `INSERT ... ON CONFLICT DO NOTHING` silently skips existing rows but
  re-runs after `downgrade base` repeat the insert with potentially
  drifted values (rotated passwords, edited emails) and require either
  `DO NOTHING` (old hash wins, surprising) or `DO UPDATE` (migrations
  mutate domain data, anti-pattern).

This decision is operationally relevant rather than primarily a
security decision, but it carries audit weight: the path by which the
first authentic identity enters the system is a controlled point that
auditors will inspect (BAIT AT 7.2, ISO 25010 *Authenticity*).

## Decision

### 1. CLI command as the canonical bootstrap path

A new top-level CLI module exposes `portfoliflow` as an entry point. The
`pyproject.toml` `[project.scripts]` table maps the command to
`cli:app`. The CLI is implemented with `typer` (selected for its
Pydantic integration and modern type-driven argument parsing).

The Phase-2 subcommands are:

- **`portfoliflow bootstrap [--email EMAIL] [--password-stdin]`** —
  strictly idempotent. Reads `SENTINEL_EMAIL` and `SENTINEL_PASSWORD`
  from the environment if the corresponding flags are absent. Inserts
  the sentinel tenant (`SENTINEL_TENANT_ID` from
  `core/tenant_constants.py`) and the sentinel user (`is_tenant_owner =
  TRUE`, `is_active = TRUE`, Argon2id-hashed password) inside a single
  transaction. If both rows already exist with matching values, no
  writes are performed. If either row is missing, the missing row is
  created. If a row exists but with different non-secret values
  (`is_tenant_owner`, `is_active`, `email`), the run fails loud rather
  than silently overwriting — drift requires explicit operator action
  via `set-password` or a future `set-attributes` subcommand.
- **`portfoliflow set-password [--email EMAIL] [--password-stdin]`** —
  rotates the password for an existing user in the sentinel tenant.
  This is the Phase-2 substitute for the email-based password reset
  flow that ADR-0036 §8 explicitly defers past Phase 2.

### 2. Connection model

The CLI connects to Postgres as the **superuser**, using
`DATABASE_URL_SUPERUSER` from `.env`. Tenant insertion is required for
`bootstrap`, and the `tenants` table policy `tenant_self_visibility`
prevents the unprivileged `portfoliflow_app` role from inserting rows
whose `id` does not match the active `app.tenant_id` — the
chicken-and-egg of "no tenant exists yet, so no GUC value is valid"
is resolved by bypassing RLS as superuser.

The CLI is the **only** code path in PortfoliFLOW permitted to use
`DATABASE_URL_SUPERUSER`. Application code (FastAPI, PyQt6 GUI,
Telegram bot) must always connect as `portfoliflow_app`. This
asymmetry is documented in `db/README.md` as part of this ADR's
implementation.

### 3. Password handling

Passwords are accepted via stdin (`--password-stdin`) or environment
(`SENTINEL_PASSWORD`). They are **never** accepted as a positional or
keyword CLI argument — shell history persistence makes that
unacceptable for institutional deployments. Stdin handling reads a
single line, strips a trailing newline, and Argon2id-hashes the
result before any logging or storage. The plaintext password never
appears in logs, exception messages, or error reports.

Argon2id parameters follow OWASP's current recommendation
(`time_cost=2`, `memory_cost=19MiB`, `parallelism=1`) and are
centralised in a `services/password_hashing.py` module so the same
parameters drive `bootstrap`, `set-password`, and the runtime login
verification path.

### 4. Idempotency contract

The bootstrap subcommand makes one of three transitions per run:

- **No-op.** Sentinel tenant and user both exist with matching
  non-secret attributes. Logs `bootstrap: no-op (sentinel tenant and
  user present)`. Exit code 0.
- **Create.** One or both rows missing. Logs each insertion
  individually (`bootstrap: created sentinel tenant <UUID>`,
  `bootstrap: created sentinel user <email>`). Exit code 0.
- **Drift detected.** A row exists with attributes that diverge from
  the bootstrap target (e.g., `is_active = FALSE`, different email).
  Logs the drift, exits with non-zero status, performs no writes.
  Resolution is an explicit follow-up command, not a silent
  reconciliation.

Password drift is **not** detected by `bootstrap`. The hash on disk
is treated as authoritative; password changes go through
`set-password` or, in Phase 3+, the application's password-change UI.
A `bootstrap` re-run does **not** rewrite the password hash — that
property is what makes operator workflows reproducible (a CI pipeline
can `bootstrap` without invalidating the developer's last password
rotation).

### 5. Failure modes

The bootstrap subcommand fails loud (non-zero exit) on:

- `SENTINEL_EMAIL` missing.
- `SENTINEL_PASSWORD` missing **and** sentinel user does not yet exist
  (an existing user does not require the password — the no-op path is
  taken).
- Drift in non-secret attributes (see §4).
- Database connection failure, schema not yet migrated (the canonical
  fix is `alembic upgrade head` first).

The deployment runbook documents the standard sequence: `alembic
upgrade head` first, `portfoliflow bootstrap` second.

### 6. Sentinel identity guarantees (preserved from ADR-0036)

- The sentinel user is **not** the Postgres superuser — three distinct
  identities (Postgres superuser, OS service account, sentinel app
  user).
- `is_tenant_owner = TRUE` inside the sentinel tenant, no cross-tenant
  rights.
- `is_active = TRUE` initially; the bootstrap does not bypass the
  application's runtime activation policy.

## Rationale

- **Separation of schema from data.** Migrations describe the shape of
  the system; bootstraps describe the initial residents of a deployed
  shape. Mixing the two is a known anti-pattern for evolving software
  in regulated environments because deployment-specific values leak
  into the schema-history record.
- **Future-compatibility with Phase 5.** Multi-tenant onboarding,
  customer-specific sentinel values, and CI-driven deployment all
  benefit from a CLI surface they can extend; an Alembic-only path
  would require a parallel CLI implementation later, doubling the
  bootstrap surface.
- **Idempotency and observability.** A CLI logs explicitly, exits with
  status codes, and integrates naturally with deployment automation. A
  startup hook hides its work behind application logs, mixing
  bootstrap with normal log volume.
- **Operations boundary.** Treating bootstrap as a deployment step
  (alongside `alembic upgrade head`) puts the operator squarely in
  the control loop. The application itself never tries to recover from
  a missing sentinel — it assumes the deployment has been completed.

## Alternatives Considered

- **Alembic seed migration.** Rejected for the schema-vs-data and
  customer-deployment reasons above. Idempotency via
  `INSERT ... ON CONFLICT DO NOTHING` does work but obscures intent and
  re-running after `downgrade base` is fragile when domain values have
  drifted between runs.
- **FastAPI startup hook.** Rejected for multi-worker race conditions,
  application-vs-deployment concern mixing, and the operational
  awkwardness of needing the application to start in order to perform
  data-deployment steps.
- **Hybrid: schema migration creates the tenant, CLI creates the
  user.** Rejected as the worst of both worlds — schema migrations
  still embed environment-specific tenant values, and the operator
  still needs a CLI step.

## Consequences

### Positive

- A clean, single-responsibility CLI surface for Phase-2 bootstrap that
  extends naturally to Phase-5 multi-tenant management.
- Schema migrations remain schema-invariant; replaying them on a fresh
  database produces an empty schema regardless of deployment.
- Operator workflows are observable, idempotent, and auditable.
- The `set-password` subcommand provides the Phase-2-acceptable
  substitute for the deferred email-based password reset flow.

### Negative

- Two-step deployment instead of one (`alembic upgrade head` followed
  by `portfoliflow bootstrap`). This is mitigated by a documented
  deployment sequence and (optionally) a `make bootstrap` target.
- The CLI introduces a new code path (`cli/`) and a new dependency
  (`typer`). Both costs are small.
- The CLI's superuser-connect privilege creates a sensitive code path
  that must be gated against accidental invocation in production
  environments. Mitigation: the deployment runbook documents that
  `DATABASE_URL_SUPERUSER` should only be present in operator
  environments, not in long-running application service environments.

### Neutral / Follow-ups

- The `cli/` module structure becomes the home for future operator
  commands (`create-tenant`, `create-user`, `rotate-secrets`,
  `migrate-tenant`).
- A future `portfoliflow status` subcommand (read-only, reports
  schema migration head, sentinel presence, configured backend) is
  natural but not Phase-2 critical.
- The Argon2id parameter centralisation in
  `services/password_hashing.py` is the seam where future password-
  hashing strategy changes (e.g., parameter increases as hardware
  improves) land.

## Implementation Notes

- **Module layout:**
  - `cli/__init__.py` — `typer.Typer()` instance exposed as `app`.
  - `cli/bootstrap.py` — `bootstrap` and `set-password` subcommands.
  - `cli/_db.py` — superuser-connect helper, single source of truth for
    `DATABASE_URL_SUPERUSER` reading.
  - `services/password_hashing.py` — Argon2id parameters and
    `hash_password(plaintext) -> str` /
    `verify_password(plaintext, hash) -> bool`.
- **`pyproject.toml`** adds:
  - `[project.scripts]` entry `portfoliflow = "cli:app"`.
  - Dependencies: `typer`, `argon2-cffi` (or `passlib[argon2]`).
- **`.env.example`** adds `SENTINEL_EMAIL=` and a comment documenting
  that `SENTINEL_PASSWORD` should be supplied via secret manager / stdin
  rather than a literal value in the file.
- **`db/README.md`** documents the canonical deployment sequence and
  the superuser-connect asymmetry.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:**
  - **Authenticity** — the controlled path by which the initial trusted
    identity enters the system.
  - **Reliability** — idempotent bootstraps support disaster-recovery
    workflows (re-running against a partially-restored database is
    safe).
  - **Operability** — explicit, observable bootstrap steps support
    deployment automation and audit trails.
- **BAIT AT 7.2 / VAIT Chapter 7.** The sentinel-user provisioning
  path is a controlled identity-onboarding event. Logs from the
  bootstrap command form the audit record of when each tenant's first
  identity was created.
- **DSGVO.** The plaintext password is never persisted, never logged,
  and is hashed before any storage; the bootstrap path is the
  earliest point at which this discipline is established.

## References

- ADR-0034 (Persistence Backend: Postgres) — schema substrate.
- ADR-0035 (Multi-Tenant Architecture: tenant_id and RLS) — sentinel
  tenant declaration (§8).
- ADR-0036 (Authentication Strategy) — sentinel user declaration
  (§6); password reset deferral (§8).
- ADR-0041 (Persistence Entry-Points: Strangler-Coexistence) —
  companion ADR on the parallel persistence surfaces.
- OWASP Argon2 password-storage cheat sheet — external reference.

---

## Revision History

| Date       | Author                       | Change |
|------------|------------------------------|--------|
| 2026-05-04 | PortfoliFLOW project owner   | Initial draft, accepted at Phase-2 kickoff. CLI-driven bootstrap with `portfoliflow bootstrap` and `portfoliflow set-password` subcommands; superuser-connect for tenant insertion; password-via-stdin discipline; explicit drift-detection rather than silent reconciliation. |
