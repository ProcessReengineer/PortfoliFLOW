# ADR-0095: Provider Credential Vault — Per-Tenant Market-Data Credentials with Staged Adoption

- **Status:** Accepted
- **Date:** 2026-07-07
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #037 — Provider Credential Management (Stage 2)
- **Tags:** market-data, security, multi-tenancy, configuration, compliance, audit

---

## Context

ADR-0091 fixed the configuration layering for the market-data providers:
**secrets come from the environment only** — never the database, never the
capability-matrix fixture. That was correct for the current operating
model (one operator, one deployment, global keys) and remains correct for
providers whose credentials are operationally the operator's own
(OpenFIGI's optional rate-limit key).

It does not scale to the product's target shape. Institutional market
data is sold per subscriber: a tenant's Bloomberg entitlement, Preqin
subscription, or PitchBook contract is **the tenant's licence**, not the
platform's. Two consequences force a decision now:

- **Licensing/compliance.** Serving tenant B's portfolio from tenant A's
  Bloomberg entitlement (or from an operator-global one) would violate
  the data-licence terms every institutional provider imposes and the
  BAIT/VAIT expectation that data sourcing is attributable per mandate.
  A *silent* fallback from "tenant credential missing" to "use the global
  key" is therefore a compliance foot-gun for this provider class.
- **Product surface.** The first question an institutional prospect asks
  a live-data demo is "how do *our* credentials get in?". The answer must
  exist as an accepted design even while the implementation arrives in
  stages.

Constraints from the existing architecture:

- `services/market_data/` is **DB-free** by regression guard
  (`tests/regression/test_market_data_layer_pure.py`). Whatever resolves
  credentials cannot live there.
- The refresh core (`services/investments/live_refresh.py`, ADR-0093)
  already runs per tenant inside `tenant_context` — it is the natural
  resolution point.
- There is **no tenant-admin area yet**. Multi-User & Permissions
  (roadmap #015) is deferred; the only administrative surfaces today are
  the super-admin area and the operator's `.env`. A credential-management
  UI therefore cannot ship first.

## Decision

### 1. The resolution contract (fixed now, provider-blind)

A `CredentialResolver` seam in the investments service layer (placement
parallel to `live_refresh.py`; **not** in `services/market_data/`)
resolves, per `(tenant, provider)`, an opaque credential payload and
hands **plain values** to the market-data factory/adapters. Adapters
never know where credentials came from; the purity guard keeps holding.

Resolution order, evaluated per provider:

1. **Tenant vault entry** (Stage 2 below) — if present and enabled.
2. **Environment fallback** — only where the provider's fallback policy
   allows it.
3. **Explicit failure** — a typed error ("no credential for provider X
   in tenant Y"), surfaced in the tick log and the ingest report; never
   a silent skip, never a silent global-key substitution.

### 2. Per-provider fallback policy is a static declaration

Each provider declares `env_fallback: allowed | forbidden` in the
capability matrix (`config/market_data_capabilities.yaml`) — the same
place its coverage lives, versioned and reviewable:

- `openfigi: allowed` — the key only raises a public rate limit; a
  global operator key is uncritical.
- `yahoo:` no credentials at all (policy irrelevant).
- `synthetic:` no credentials (fixture path is explicitly a test-session
  env concern, ADR-0093).
- `bloomberg`, `preqin`, `pitchbook` (future adapters): **forbidden** —
  tenant-licensed data requires tenant credentials, full stop.

The policy is a **fixed invariant per provider class, not a config
knob** a deployment can loosen (same posture as Excel precedence,
ADR-0092).

### 3. Stage 1 (now): environment-only resolution

The resolver ships with the environment source only. Env variable
naming follows the existing convention (provider-specific, documented in
`.env.example`; `OPENFIGI_API_KEY` stays as-is). Multi-field credentials
(future Bloomberg DL account/key pairs) use one prefixed variable per
field. Stage 1 changes **no schema** and adds **no migration**; it
shapes the seam so Stage 2 slots in behind an existing interface.

### 4. Stage 2 (own feature session, own roadmap entry): the tenant vault

Fixed as the target design so the later session implements against an
accepted decision, not a fresh debate:

- **Table `provider_credentials`** (tenant-scoped, RLS via
  `apply_tenant_rls`): `id`, `tenant_id`, `provider` TEXT (values from
  the capability matrix's provider set), `payload_ciphertext` (encrypted
  JSONB serialisation — JSONB-shaped so per-provider field sets need no
  schema change), `enabled` BOOLEAN NOT NULL, `user_id` UUID **nullable**
  (reserved: per-user credentials only if/when Bloomberg Desktop-API
  licensing semantics force them — not implemented before then), audit
  columns per house idiom. `UNIQUE (tenant_id, provider)` (per-user
  uniqueness added only with per-user semantics).
- **Encryption:** application-level symmetric encryption (Fernet) of the
  serialised payload, master key from the environment
  (`CREDENTIAL_VAULT_MASTER_KEY`), never stored in the database.
  Key rotation is a **documented operator procedure** via a re-encrypt
  CLI command (decrypt with old key, encrypt with new, single
  transaction), not an automatic mechanism. This is the deliberate
  BAIT-appropriate minimum: documented key custody, documented rotation,
  no KMS/HSM dependency at this scale.
- **Administration:** managed by a **tenant admin** in a tenant-admin
  area that does not exist yet and must be created as part of the Stage-2
  feature (its authorisation model depends on roadmap #015's role
  semantics — Stage 2 therefore lists #015 as a dependency or ships a
  minimal owner-role gate as its vanguard). UI semantics are fixed now:
  credential fields are **write-only** (stored values are never rendered
  back; display shows provider, enabled flag, set/unset status and at
  most a last-4 hint captured at write time), and payloads never appear
  in logs, error messages, or audit rows.
- Until Stage 2 lands, Stage 1's environment source is the only source;
  the resolver makes the layering visible in its log line ("resolved
  from env") so later vault adoption is observable per tenant.

### 5. What this ADR does not decide

- No KMS/HSM, no external secret manager — revisit if deployment moves
  beyond the single-operator Hetzner topology.
- No per-user credentials (column reserved, semantics deferred).
- No proxying/pooling of one tenant's session for another — categorically
  out, per §2.

## Consequences

- The demo-critical question ("how do our credentials get in?") has an
  accepted architectural answer with a visible seam in code, even while
  administration remains operator-side.
- Live usage of tenant-licensed providers (Bloomberg/Preqin/PitchBook)
  is gated on Stage 2 **by design** — an env-only deployment cannot even
  accidentally serve tenant-licensed data from a global key, because the
  fallback policy forbids the path.
- A new roadmap entry (Provider Credential Management — Stage 2: vault
  table, encryption, re-encrypt CLI, tenant-admin surface) must be
  raised; the tenant-admin area becomes its own scoped feature with a
  dependency note on #015.
- One more master secret (`CREDENTIAL_VAULT_MASTER_KEY`) joins the
  operator's key custody duties at Stage 2 — documented alongside the
  existing secret-handling notes.
- The resolver adds one indirection between refresh core and factory;
  accepted as the price of keeping `services/market_data/` pure and
  provider-blind.

## Alternatives considered

- **Keep environment-only forever.** Rejected: cannot express per-tenant
  licences; blocks the product's multi-tenant premise; forces exactly
  the silent-global-key substitution §2 forbids.
- **In-database encryption via pgcrypto.** Rejected: the key would
  transit SQL text and appear in statement logging/pg_stat_statements
  surfaces; application-level encryption keeps key material out of the
  database entirely.
- **External secret manager (Vault/KMS) now.** Rejected as premature for
  the single-server topology; the Fernet-plus-documented-rotation
  posture is auditable and proportionate. Revisit with deployment
  growth.
- **Per-tenant `.env` files.** Rejected: unmanageable operationally, no
  RLS/audit story, still operator-administered — solves neither problem.
- **Fallback policy as a per-deployment config knob.** Rejected: a
  compliance invariant must not be loosenable by configuration (ADR-0092
  precedent).

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-08-03 | PortfoliFLOW project owner | §4 (Stage-2 storage design) superseded by ADR-0112 §2: the `provider_credentials` table is absorbed into the general `scoped_settings` table (per-field rows, Fernet for secret rows only). §1–§3 remain authoritative and unchanged. Status field unchanged — the ADR is partially superseded, not replaced. |
