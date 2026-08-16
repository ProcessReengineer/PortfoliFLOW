# ADR-0112: Scoped Settings & Credential Architecture — Application / Tenant / User

- **Status:** Accepted
- **Date:** 2026-08-03
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #055 — Scoped Settings & Credential Architecture
- **Supersedes / amends:** supersedes **ADR-0095 §4** (Stage-2 storage design: the
  `provider_credentials` table is absorbed into `scoped_settings`, §2 below) while
  ADR-0095 §1–§3 (resolution contract, per-provider fallback policy, environment
  source declaration) **remain authoritative and unchanged**; supersedes the
  **ADR-0052** persistence posture ("runtime edits live in the process only; `.env`
  is the canonical persistence surface") once §6's management surface lands
- **Tags:** configuration, security, multi-tenancy, credentials, llm, telegram,
  voice, admin, compliance, audit, deployment

---

## Context

PortfoliFLOW's configuration today is environment-only, read through four parallel
planes: `web/settings.py` (`WebSettings`, pydantic-settings), `bot/config.py`
(`BotSettings`, own dotenv load), `services/voice/config.py` (`VoiceConfig`, own
dotenv load), and `core/config.py` (the Qt-flavoured plane). Every credential —
OpenRouter key, Telegram bot token, voice API keys, the optional OpenFIGI key —
is an operator-global environment variable. That was the correct posture for a
single-operator deployment; it does not carry a hosted multi-tenant SaaS on the
Hetzner topology, where:

- **LLM cost and identity must be attributable per tenant.** Shirley and Irene
  run against one `OPENROUTER_API_KEY` for every tenant. A hosted instance needs
  tenant-owned keys (their spend, their rate limits, their provider relationship)
  with the operator key at most as an explicit application-scope fallback.
- **Telegram is structurally single-tenant.** One bot token, one whitelist
  (`TELEGRAM_ALLOWED_USER_IDS`), one injected tenant. A hosted instance needs one
  bot per tenant and a user↔chat binding that does not route through the operator.
- **Tenant-licensed market data is already gated by design.** ADR-0095 §2 declares
  `env_fallback: forbidden` for Bloomberg Server-API / B-PIPE / Data-License class
  providers — those adapters *cannot ship* until a tenant-scope credential source
  exists.

Three verified facts from the current tree shape this decision:

1. **The Stage-1 `CredentialResolver` has zero productive call sites.** It ships
   as a tested seam (ordered source list, three outcomes, masked logging,
   `tenant_id` threaded), but `resolve_figi` — its intended first consumer — is
   itself not yet wired into any flow (recorded in the roadmap change-log,
   2026-07-07). "Migrating existing call sites" is therefore an empty set for
   market data; the substantive migration work is wiring **LLM and Telegram**
   through the resolver for the first time.
2. **`AIServiceCore` is a process-wide, tenant-blind singleton.** The web
   lifespan configures it once from `.env`; the Irene tick reconfigures the same
   singleton per tick from `.env`; the Telegram bot builds its own instance from
   `BotSettings`. Tenant-scoped LLM keys are incompatible with "configure once
   per process" — this ADR must decide the client-construction model, not only
   the storage.
3. **There is no `application_settings` surface to reuse.** The Admin area's
   "Application Settings" section is a `planned` placeholder with no route; the
   `modules/admin/application_settings.py` module is a Qt registry stub. The
   actual authorisation precedents are `require_role("owner")` on
   `POST /admin/ai-settings` (ADR-0052) and `require_super_admin` on
   `/super-admin/*` (ADR-0064). `require_role` is strict — no implicit promotion;
   routes list every permitted role.

Constraints carried forward: `services/market_data/` and `services/analytics/`
stay DB-free (regression guards); accepted ADRs are immutable (corrections via
successors — hence the explicit partial supersession of ADR-0095 above); the
Multi-User item **#015 B1c** (per-role tool-trust overlay, ADR-0022 §4) is open
and explicitly *not* pulled forward by this ADR; the in-process Telegram bot's
single-`getUpdates`-consumer-per-token constraint (documented in `web/main.py`)
must survive any multi-bot design.

## Decision

### 1. Scope model: three scopes, explicit per-setting resolution chains

Settings and credentials resolve across three scopes:

```
application   — the deployment (source in v1: the environment / .env)
tenant        — one tenant           (source: scoped_settings rows, tenant scope)
user          — one user in a tenant (source: scoped_settings rows, user scope)
```

- The **default chain** is `user → tenant → application(env)`: the most specific
  scope that holds a value wins. This generalises ADR-0095 §1's
  `vault → env → explicit failure` order — the vault simply gains an inner
  ordering (user before tenant) and the env keeps its place as the
  application-scope source.
- **Per-provider `env_fallback: forbidden` semantics are preserved unchanged**
  (ADR-0095 §2): for a forbidden-policy provider the chain is
  `user → tenant → explicit failure` — the application link is absent, and no
  deployment knob can restore it.
- Every setting is either **pinned** (exactly one scope; no chain) or **chained**
  (resolves along its declared chain). The classification is recorded per setting
  in **Annex A** and is part of this ADR's accepted content: adding a setting or
  changing its chain is an annex amendment via successor ADR or the annex's own
  change log, never an ad-hoc code decision.
- **No cross-scope field mixing.** A multi-field credential (e.g. a future
  Bloomberg DL account/key pair) must resolve *all* of its declared fields from
  **one** scope level. A tenant that has set only one of two fields does not get
  the second field from the environment — the tenant source declines (mirroring
  the Stage-1 env-source rule "an incomplete credential is treated as absent"),
  and resolution falls through to the next scope as a whole.

### 2. Storage: one table, `scoped_settings` (supersedes ADR-0095 §4)

One table absorbs both concerns — general settings and provider credentials.
ADR-0095 §4's separate `provider_credentials` design is **not built**; this
section is its successor.

```sql
scoped_settings (
    id               UUID PK DEFAULT gen_random_uuid(),
    scope            TEXT    NOT NULL CHECK (scope IN ('application','tenant','user')),
    tenant_id        UUID    NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    user_id          UUID    NULL REFERENCES users(id)   ON DELETE RESTRICT,
    provider         TEXT    NOT NULL,       -- taxonomy key, validated in code (§3)
    key              TEXT    NOT NULL,       -- field name, e.g. 'api_key', 'model', 'bot_token'
    is_secret        BOOLEAN NOT NULL,
    value_plain      TEXT    NULL,           -- config rows only
    value_ciphertext BYTEA   NULL,           -- secret rows only (Fernet token)
    secret_hint      TEXT    NULL,           -- at most last 4 chars, captured at write time
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at / updated_at / audit columns per house idiom,

    CHECK ((scope = 'application') = (tenant_id IS NULL)),
    CHECK ((scope = 'user')        = (user_id  IS NOT NULL)),
    CHECK (is_secret = (value_ciphertext IS NOT NULL)
           AND is_secret = (value_plain IS NULL)),
    UNIQUE NULLS NOT DISTINCT (scope, tenant_id, user_id, provider, key)
)
```

Decisions folded into the shape:

- **Key-value rows, not a JSONB payload.** ADR-0095 §4 specified one encrypted
  JSONB payload per `(tenant, provider)`. This ADR replaces that with **one row
  per field**: a multi-field credential is several rows sharing
  `(scope, tenant, user, provider)`. Rationale: per-field rows make the
  completeness rule (§1) and the write-only/masked display (§6) directly
  expressible, keep non-secret fields (model names, base URLs) unencrypted and
  greppable for support, and let config and secret fields of one provider carry
  different update timestamps for audit. The §1 no-cross-scope-mixing rule
  removes the one hazard per-field rows introduce.
- **Uniqueness** uses `UNIQUE NULLS NOT DISTINCT` (PostgreSQL 15+; the podman
  images in use qualify). If a deployment ever needs to run on <15, the
  equivalent coalesce-based unique index is the documented fallback — a
  deployment note, not a schema fork.
- **RLS:** `apply_tenant_rls('scoped_settings')` attaches the standard
  `tenant_isolation` policy. `user`-scope rows are additionally filtered by the
  repository layer on `user_id` (the `app.user_id` GUC that `tenant_context`
  already sets makes a future policy-level binding possible; v1 keeps the
  house-standard tenant-policy-plus-repository-filter idiom). `application`-scope
  rows (`tenant_id IS NULL`) are **unreachable by the app role under the tenant
  policy by construction** — deliberate: in v1 the application scope's source is
  the environment, no application rows are written, and the schema carries the
  scope value from day one so a future ADR can wire application rows through the
  superuser path without a table change (no structural debt).
- **Encryption:** application-level Fernet for `is_secret` rows only, exactly the
  ADR-0095 §4 posture: master key from `CREDENTIAL_VAULT_MASTER_KEY` (environment
  only, **never** in the database), no KMS/HSM at this scale. The `cryptography`
  package joins `pyproject.toml` dependencies (it is not currently declared).
- **Missing master key = vault disabled, loudly.** When
  `CREDENTIAL_VAULT_MASTER_KEY` is unset, the resolver's DB source is disabled
  (one WARNING at first use), reads of secret rows are not attempted, and writes
  through the §6 surface fail with a typed, operator-readable error. The
  application never runs a silent plaintext mode and never half-serves a vault.
- **Rotation:** `portfoliflow vault-rotate-key` — old key from env or
  `--old-key-stdin`, new key via `--new-key-stdin`; decrypt-and-re-encrypt of
  **all** `is_secret` rows in a single transaction on the **superuser engine**
  (cross-tenant by nature, mirroring the bootstrap/Alembic CLI pattern;
  RLS-bypassing reads are sanctioned for this command the way they are for
  `inspect-tenant`). A companion `portfoliflow vault-generate-key` emits a fresh
  Fernet key for the operator's custody procedure (documented under
  `docs/deploy/`). Rotation is a documented operator procedure, not an automatic
  mechanism.
- **Migration number is claimed at implementation time (F1), never here.**

### 3. Provider taxonomy v1

The `provider` column's value set is validated **in code at the write path**
against the declared taxonomy, not by a CHECK constraint — the provider set grows
with adapters (ADR-0091/0096 precedent) and is already declared across the
capability matrix and this taxonomy; a CHECK would force a migration per adapter
without adding a second source of truth worth having. The taxonomy ships as:

| Provider key | Fields (key → kind) | Scopes served in v1 | Chain |
|---|---|---|---|
| `openfigi` | `api_key` (secret) | tenant | tenant → application(env) — `env_fallback: allowed`, `optional` |
| `bloomberg_serverapi` (and future `preqin`, `pitchbook`, `bloomberg_dl`) | per adapter ADR | **schema-ready only** | user/tenant → failure (`forbidden`) |
| `openrouter` | `api_key` (secret), `model` (config), `base_url` (config), `irene_model` (config) | tenant (user-scope key **in the model**, no UI in v1) | user → tenant → application(env) |
| `telegram` | `bot_token` (secret, tenant), `chat_id` (config, user), `pairing` internals (§5) | designed here, **implemented in F5** | token: tenant → application(env, current single-bot mode); chat binding: user only |
| `voice_stt` / `voice_tts` | `api_key` (secret), `model`, `base_url`, `voice` (config) | **pinned application(env)** in v1 | taxonomy-extensible later |

Honesty note, recorded deliberately: **v1 contains no live credentialed
market-data adapter.** Bloomberg Desktop-API declares `credentials: none`
(ADR-0091 — the Terminal is the auth boundary) and the credentialed Server-API /
B-PIPE / DL variants are future #036 adapters. The market-data half of this ADR
therefore delivers the tenant-scope **source** (schema + resolver) that those
adapters are gated on, plus the one live consumer that exists today (`openfigi`).
Nothing here claims a Bloomberg vault consumer that does not exist.

### 4. Resolver: one façade, and the LLM client model that makes it usable

`CredentialResolver` becomes the **single credential façade for every provider
class** — market data, LLM, voice (future), Telegram. Two changes:

**4a. The DB source prepends.** A `("vault", …)` source is inserted ahead of
`("env", …)` in the existing ordered source list — precisely the extension point
Stage 1 encoded. The vault source resolves `user → tenant` (per §1) inside the
caller's `tenant_context`; the env source keeps serving the application scope
where the policy allows. The no-silent-fallback rule, the three outcomes, the
masked structured log line (now stating `source=vault-user | vault-tenant | env`)
all carry over unchanged. The completeness and no-cross-scope-mixing rules of §1
are enforced here.

**4b. LLM clients are resolved per turn/beat, not configured per process.**
This is the structural decision the storage alone does not give us. The
process-wide `AIServiceCore` singleton keeps what is genuinely process-wide —
the shared `ToolRegistry`, the `_TURN_LOCK` turn serialisation (ADR-0031), the
system-prompt loading — but **stops owning the one global OpenRouter key** once
F4 lands. Instead:

- The chat route, the Irene beat, and the bot handler resolve the `openrouter`
  credential through `CredentialResolver` **inside the tenant context of the
  turn/beat**, and the core serves an OpenAI-compatible client for exactly that
  resolution (constructed per resolution, or served from a tenant-keyed client
  cache behind the façade — an implementation detail F4 may choose; the contract
  is "the client used for a turn is the one the tenant's resolution produced").
- **Single-tenant deployments behave unchanged:** with no vault rows, every
  resolution falls through to the application scope and produces the same client
  the lifespan configures today.
- **The Irene tick generalises its tolerant stance per tenant.** Today "no API
  key → no-op, exit 0". After F4: resolution runs per due tenant inside the beat
  transaction; a tenant with no resolvable key is **skipped with a log line**
  (consistent with the existing per-tenant failure isolation) and the tick
  continues; the tick as a whole no-ops only when *no* tenant resolves a key.
- The `/admin#ai-settings` runtime-mutation path (ADR-0052) is retired by F3/F4
  in favour of the §6 surface writing tenant-scope rows; its "edits live in the
  process only, `.env` is canonical" banner — explicitly written to hold "until
  per-user settings land" — is superseded by this ADR.

### 5. Telegram target design (decided here, implemented as F5)

- **One aiogram process, multiplexing per-tenant bot tokens**: the existing
  in-process bot thread grows to N pollers — one dispatcher per token, each its
  own `getUpdates` consumer, so the one-consumer-per-token constraint holds *per
  token* and the documented **single-uvicorn-worker assumption stands
  unchanged**. A failing tenant bot (revoked token, network) is isolated to its
  dispatcher — the existing "the bot is a convenience; its failure never blocks
  the web" degradation rule, applied per tenant.
- **Bot token = tenant scope** (`telegram.bot_token`, secret row). Tenants bring
  their own BotFather token; the operator's global token remains only as the
  application-scope fallback that powers the **current single-bot mode** during
  the transition.
- **User↔chat binding = user scope via pairing code.** The web surface (§6, user
  scope) issues a short-lived, single-use pairing code; the user sends
  `/pair <code>` to their tenant's bot; the handler validates the code and writes
  the `telegram.chat_id` user-scope row. This replaces per-user entries in
  `TELEGRAM_ALLOWED_USER_IDS`; that variable becomes the application-scope
  fallback for the current single-bot mode and is deprecated once F5 completes.
  Authorisation on incoming messages becomes "chat_id has a user-scope binding in
  this bot's tenant" — silent drop otherwise, exactly today's leak-nothing
  posture.
- The bot's own `AIServiceCore` instance goes through the §4b resolution the same
  way the web chat does (its tenant is the dispatcher's tenant).
- F5 carries its **own gate** and, if the multiplexing work grows beyond this
  section's shape, its **own successor ADR** — the schema (§2/§3) carries the
  design from day one either way.

### 6. Admin surface: "Providers & Credentials" (corrected authorisation)

The Admin area gains a **Providers & Credentials** module (registry entry +
lazy HTMX section, the house pattern):

- **Tenant scope:** managed by tenant admins — gated
  `Depends(require_role("owner"))`, the verified ADR-0052 precedent (there is no
  `application_settings` guard to reuse; that section is a placeholder). CSRF +
  session per house idiom.
- **User scope:** each user manages **their own** rows — gated
  `require_session`; the repository writes/reads only `user_id = session.user_id`.
- **Application scope:** no web write surface in v1 — the environment stays the
  application-scope source (§1), operator-managed. (A future super-admin surface
  would ride `require_super_admin`; out of scope here.)
- **Secrets are write-only with masked display** (per the absorbed ADR-0095 §4
  contract): stored values are never rendered back; display shows provider,
  key, enabled flag, set/unset status, and at most the `secret_hint` last-4
  captured at write time. Secret values never appear in logs, error messages, or
  audit rows.
- **Boundary to #015 B1c, stated explicitly:** this surface uses the *existing*
  role model (owner/member/auditor + session identity) and nothing finer. The
  per-role tool-trust overlay (ADR-0022 §4) remains #015 B1c and is neither
  implemented nor prejudged here; when B1c lands, this surface adopts its
  semantics without schema change.

### 7. What this ADR does not decide

- No KMS/HSM, no external secret manager (unchanged from ADR-0095 §5; revisit
  beyond the single-server Hetzner topology).
- No user-scope UI for the OpenRouter key in v1 (model carries it; §3).
- No new market-data adapters and no #036 scope (the vault is their
  prerequisite, not their implementation).
- No #015 B1c semantics (§6).
- No change to the Qt/QSettings configuration plane (it sunsets with #016).
- No CD/deployment automation (#025); only the `.env.example` and
  `docs/deploy/` documentation duties named in Consequences.

## Consequences

- **New dependency:** `cryptography` (Fernet) enters `pyproject.toml` —
  wheel-only install, unproblematic on Tumbleweed and the Hetzner target.
- **New master secret:** `CREDENTIAL_VAULT_MASTER_KEY` joins the operator's key
  custody duties: generation (`vault-generate-key`), storage outside the
  repository, rotation procedure (`vault-rotate-key`), and the
  missing-key-means-disabled-vault behaviour are documented under
  `docs/deploy/` in F0/F1.
- **`.env.example` gains** the previously undocumented `WEB_HOST` / `WEB_PORT` /
  `SESSION_COOKIE_SECURE` (the latter **must be `true`** behind TLS on the
  hosted instance) and, at F1, `CREDENTIAL_VAULT_MASTER_KEY`.
- **ADR-0052's runtime-mutation surface is retired** by F3/F4; the persistence
  banner is replaced by a scope indicator ("applies to: tenant …").
- **The Irene tick's no-op contract refines** from process-level to per-tenant
  (§4b) — observable in the tick log, covered by F4's tests.
- **ADR-0095 remains the resolution-contract authority** (§1–§3); its §4 storage
  spec is formally superseded here. The `provider_credentials` table is never
  created.
- One more indirection at LLM-turn start (credential resolution inside the
  tenant context); accepted — it is the same price ADR-0095 accepted for market
  data, and it buys tenant attribution of LLM identity and spend.
- The single-uvicorn-worker deployment assumption is **retained and now
  load-bearing for N bots**; the deploy README states it.

## Alternatives considered

- **Two-table storage (`scoped_settings` + `provider_credentials` per ADR-0095
  §4).** Rejected: the two tables would share scope columns, RLS policy,
  encryption machinery, uniqueness shape, and management surface, differing only
  in payload layout — a duplicated apparatus for one concern. Absorbing the
  credential rows into the general table (with `is_secret` + per-field rows)
  keeps one resolver source, one rotation CLI, one admin surface, and one RLS
  audit story. The cost — credentials and plain settings sharing a table — is
  mitigated by the CHECK-enforced ciphertext/plain exclusivity and the
  write-path taxonomy validation.
- **One encrypted JSONB payload per `(tenant, provider)` (the ADR-0095 §4
  shape).** Rejected: it hides field-level set/unset state behind decryption
  (breaking the masked display's "which fields are set" without touching
  plaintext), forces non-secret fields (model names, base URLs) into ciphertext
  or into a second mechanism, and gives multi-field completeness checking no
  natural seam. Per-field rows express §1's completeness and no-mixing rules
  directly; the uniqueness constraint keeps them consistent.
- **Per-tenant bot processes (one OS process per tenant bot).** Rejected:
  multiplied memory and operational surface (N systemd units, N log streams, N
  failure modes) on a single-server topology, a new IPC problem for the shared
  Postgres/AI seams, and no isolation benefit the per-dispatcher isolation of §5
  does not already provide — aiogram v3 is asyncio-native and N pollers in one
  loop is its supported shape. Revisit only if a tenant's bot load ever justifies
  a dedicated worker, which is not a v1 concern.
- **Reconfiguring the process-global `AIServiceCore` per turn (keep "configure
  once" semantics, call `configure()` with the tenant's key at turn start).**
  Rejected: it turns tenant credentials into racy global mutable state — the
  Irene tick and the web chat run in different processes today but the bot shares
  the web process, `_TURN_LOCK` serialises *turns*, not every consumer of the
  core's configuration, and an exception between configure-and-restore leaks one
  tenant's client to the next caller. Per-turn/beat **resolution** with
  per-tenant client construction (§4b) keeps the shared machinery shared and the
  credential state scoped to exactly the resolution that produced it.

---

## Annex A — Settings inventory (verified against the 2026-08-03 snapshot)

Legend: **P** = pinned, **C** = chained. Chain notation omits scopes a policy
forbids. "Today's source" names the module that reads the variable.

| Setting / env variable | Today's source (reader) | Provider · key | Scope target | P/C · chain | Secret? |
|---|---|---|---|---|---|
| `DATABASE_URL` | `WebSettings`, `bot/config.py`, fixtures | — (infrastructure) | application | P | yes |
| `DATABASE_URL_SUPERUSER` | `WebSettings`, `cli/_db`, fixtures | — (infrastructure) | application | P | yes |
| `OPENROUTER_API_KEY` | `WebSettings`, `bot/config.py`, `cli/irene_tick.py` | `openrouter` · `api_key` | user/tenant/application | C · user → tenant → application(env) | yes |
| `OPENROUTER_BASE_URL` | same three readers | `openrouter` · `base_url` | tenant/application | C · tenant → application(env) | no |
| `SHIRLEY_MODEL` | same three readers | `openrouter` · `model` | user/tenant/application | C · user → tenant → application(env) | no |
| `IRENE_MODEL` | `cli/irene_tick.py::_resolve_model` | `openrouter` · `irene_model` | tenant/application | C · tenant → application(env); intra-scope precedence irene_model → model preserved | no |
| `TELEGRAM_BOT_ENABLED` | `bot/config.py` | `telegram` · `enabled` | tenant/application | C · tenant → application(env) (F5) | no |
| `TELEGRAM_BOT_TOKEN` | `bot/config.py` | `telegram` · `bot_token` | tenant/application | C · tenant → application(env, single-bot transition mode) | yes |
| `TELEGRAM_ALLOWED_USER_IDS` | `bot/config.py` | `telegram` (legacy whitelist) | application | P (fallback for single-bot mode; **deprecated by F5** pairing bindings) | no |
| — (new) pairing binding | — | `telegram` · `chat_id` | user | P (user only) | no |
| `SHIRLEY_BOT_TENANT_SUBDOMAIN` | `bot/config.py`, lifespan | `telegram` · transition config | application | P (retires with F5) | no |
| `VOICE_ENABLED`, `VOICE_STT_PROVIDER/MODEL/BASE_URL`, `VOICE_TTS_PROVIDER/MODEL/VOICE` | `services/voice/config.py` | `voice_stt`/`voice_tts` · config fields | application | P (v1; taxonomy-extensible) | no |
| `VOICE_STT_API_KEY`, `VOICE_TTS_API_KEY` | `services/voice/config.py` | `voice_stt`/`voice_tts` · `api_key` | application | P (v1) | yes |
| `OPENFIGI_API_KEY` | `CredentialResolver._ENV_CREDENTIAL_FIELDS` (sole reader) | `openfigi` · `api_key` | tenant/application | C · tenant → application(env); `optional: true` | yes |
| `BLPAPI_HOST`, `BLPAPI_PORT` | market-data factory | — (connection settings, ADR-0091; not credentials) | application | P | no |
| `MARKET_DATA_SYNTHETIC_FIXTURE` | factory (test sessions only) | — | application | P | no |
| Bootstrap family: `OWNER_EMAIL/PASSWORD/DISPLAY_NAME`, `SUPER_ADMIN_*`, `SENTINEL_*` (alias) | `cli/bootstrap.py` | — (CLI-only seed) | application | P | partly |
| `LOCAL_DEV_TENANT_SUBDOMAIN` | tenant resolver | — | application | P | no |
| `WEB_MAX_UPLOAD_SIZE_MB`, `CASE_ATTACHMENT_MAX_BYTES`, `CASE_ATTACHMENT_MAX_COUNT` | route-time reads | — | application | P | no |
| `WEB_HOST`, `WEB_PORT`, `SESSION_COOKIE_SECURE` | `WebSettings` (pydantic fields; **currently missing from `.env.example`** — F0 fixes) | — | application | P | no |
| `APP_NAME`, `DEBUG`, `LOG_LEVEL`, `DATA_DIR`, `DB_URL`, `APP_DB_ROLE`, `BUILD_SHA` | `core/config.py` (Qt plane), shell/session | — | application | P | no |
| `CREDENTIAL_VAULT_MASTER_KEY` (**new**, F1) | vault encryption helpers | — (master secret) | application | P · **never stored in DB** | yes |

Chained rows are the model's v1 payload: `openrouter` (F4), `telegram` (F5),
`openfigi` (F2 makes the tenant scope live for the one existing consumer).
Everything pinned stays exactly where it is — the annex exists so that "stays
env" is a recorded decision per setting, not an omission.

## Annex B — Strand table (F0 … F5)

Each sub-strand: one Claude-Code prompt (verify-first → scoped implementation →
explicit not-in-scope → operator action block), restricted test scope, operator
gate before the next strand. Migration and any further ADR numbers are claimed
at implementation time.

| Strand | Scope | Key deliverables | Gate |
|---|---|---|---|
| **F0** | Pre-flight & documentation | `.env.example` gains `WEB_HOST`/`WEB_PORT`/`SESSION_COOKIE_SECURE`; `docs/deploy/` key-custody note skeleton (master-key generation/storage/rotation, missing-key behaviour); annex cross-checked against the tree one last time | doc review |
| **F1** | Schema + vault machinery | `scoped_settings` migration (next free `b0NN`) + ORM model + repository (tenant RLS, user-filter idiom) + RLS/roundtrip tests; `cryptography` dependency; Fernet encrypt/decrypt helpers; `vault-generate-key` / `vault-rotate-key` CLI (superuser engine, single transaction) + tests | restricted suite + migration roundtrip green |
| **F2** | Resolver vault source | `("vault", …)` source prepended; user→tenant inner order; completeness + no-cross-scope-mixing rules; `source=vault-user/vault-tenant/env` log line; missing-master-key = disabled source (WARNING); openfigi tenant scope live end-to-end | resolver suite green |
| **F3** | Admin surface | "Providers & Credentials" module (Admin area, registry + lazy section); tenant scope via `require_role("owner")`, user scope via `require_session` self-service; write-only/masked/last-4; retires the ADR-0052 AI-settings mutation form + banner | UI gate + suite |
| **F4** | LLM through the resolver | Per-turn resolution in the chat route; per-beat per-tenant resolution in the Irene tick (skip-with-log semantics); bot core through resolution; tenant-keyed client construction behind the `AIServiceCore` façade; single-tenant no-vault behaviour proven unchanged | full-suite operator gate |
| **F5** | Telegram multi-bot | Dispatcher-per-token multiplexing (single-worker constraint retained); tenant token rows; pairing-code issue surface + `/pair` handler + user-scope binding; `TELEGRAM_ALLOWED_USER_IDS` demoted to deprecated application fallback | own gate; successor ADR if the shape grows |

---

## Revision history

| Date | Author | Change |
|---|---|---|
| 2026-08-03 | PortfoliFLOW project owner | Initial draft (Proposed) — Chat F, Turn 2. |
| 2026-08-03 | PortfoliFLOW project owner | Status moved to **Accepted** (Chat F, Turn 3). Verified Turn-1 corrections are folded in: §6 authorisation reuses the ADR-0052 `require_role("owner")` precedent (no `application_settings` guard exists to reuse); §4b decides per-turn/beat LLM client resolution against the tenant-blind singleton; §2 formally supersedes ADR-0095 §4 (per-field rows replace the JSONB payload; `provider_credentials` is never created). Implementation proceeds per Annex B (F0…F5); migration and any successor ADR numbers are claimed at implementation time. |
