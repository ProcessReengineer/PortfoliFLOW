# ADR-0118: Voice Providers in the Scoped-Settings Taxonomy — Per-Tenant Voice Credentials & Settings

- **Status:** Accepted (2026-08-12)
- **Date:** 2026-08-12
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #059 — Voice Configurability (per-tenant voice
  credentials & settings)
- **Supersedes / amends:** **annex amendment to ADR-0112 §3** (the taxonomy
  table's `voice_stt` / `voice_tts` row: "pinned application(env) in v1,
  taxonomy-extensible later" — this ADR is that extension). ADR-0112 itself
  remains immutable and otherwise unchanged. ADR-0076 (voice service) remains
  authoritative for the audio pipeline, the provider protocol, the per-call
  client lifecycle and the Groq-via-`base_url` mechanism; only its
  configuration posture ("the voice service reads its own configuration from
  the environment") is superseded for the tenant-facing fields declared here.
- **Tags:** voice, configuration, security, multi-tenancy, credentials, admin,
  telegram, shirley

---

## Context

ADR-0076 shipped voice I/O for Shirley (turn-based STT in, post-completion TTS
out, on both the web surface and the Telegram bot) with configuration read from
the environment: nine fields on `services/voice/config.py::VoiceConfig`
(`enabled`; STT `provider`/`model`/`api_key`/`base_url`; TTS
`provider`/`model`/`voice`/`api_key`), a lazy module-level config singleton, a
lazy module-level provider singleton (`services/voice/factory.py`), and loud
validation gated on `enabled`. STT and TTS are deliberately configured
**independently** so providers can be mixed — the canonical case being Groq STT
(OpenAI-wire-compatible, reached via `stt_base_url`) with OpenAI TTS.

ADR-0112 then built the scoped-settings apparatus — the
application/tenant/user scope model (§1), the `scoped_settings` vault (§2), the
provider taxonomy (§3), the single credential façade `CredentialResolver` with
its `resolve` / `resolve_config` halves (§4/4b), and the Admin → Providers &
Credentials surface (§6) — and moved OpenRouter and Telegram into it. Voice was
**deliberately pinned application-scope in v1**, with the §3 table already
sketching the future shape: two providers `voice_stt` / `voice_tts`,
"taxonomy-extensible later".

The consequence of that pin in a multi-tenant deployment: every tenant's voice
usage runs on the operator's central API keys at the operator's cost, voice is
on or off for the whole process, and Shirley's persona voice is one global
value. This is precisely the economic-and-isolation logic that moved OpenRouter
and Telegram into `scoped_settings` (#055) — per-tenant attribution of cost and
identity, per-tenant enablement, no restart to apply. The F4 `ResolvedLLM` seam
(per-turn resolution inside the tenant context, value object with masked repr,
nothing cached, nothing stashed) is the established pattern for turning a
startup-singleton consumer into per-request resolution; the
`OpenAIVoiceProvider`'s existing per-call `AsyncOpenAI` client discipline makes
a per-request provider instance effectively free.

Verified preconditions (2026-08-12 snapshot): the taxonomy's constructor rule
(secret fields may not declare application scope), the unit-chaining rule for a
provider's secret fields, the individual chaining of config fields, the
TX-02/TX-06 pinning suites, the F4 per-turn resolution in the web chat route,
the Telegram handler and the Irene beat, and the taxonomy-driven Admin cards
with `_PROVIDER_DESCRIPTIONS` / `_FIELD_HINTS` / freshness pills — all present
and load-bearing.

## Decision

### 1. Three taxonomy declarations, not one and not two

The taxonomy gains **three** declarations, all with `managed_by_matrix=False`,
`env_fallback=True`, `optional=False`:

| Provider key | Fields (key → kind) | Scopes | Env link |
|---|---|---|---|
| `voice` | `enabled` (config) | tenant | `VOICE_ENABLED` (`_ENV_CONFIG_FIELDS`) |
| `voice_stt` | `api_key` (secret) · `model` (config) · `base_url` (config) | tenant (all fields) | `VOICE_STT_API_KEY` (`_ENV_CREDENTIAL_FIELDS`) · `VOICE_STT_MODEL`, `VOICE_STT_BASE_URL` (`_ENV_CONFIG_FIELDS`) |
| `voice_tts` | `api_key` (secret) · `model` (config) · `voice` (config) | tenant (all fields) | `VOICE_TTS_API_KEY` (`_ENV_CREDENTIAL_FIELDS`) · `VOICE_TTS_MODEL`, `VOICE_TTS_VOICE` (`_ENV_CONFIG_FIELDS`) |

**Why the STT/TTS split.** ADR-0112 §1 chains a provider's secret fields as a
unit: one scope holds every secret field or that scope declines as a whole. A
single `voice` provider carrying both `stt_api_key` and `tts_api_key` would
therefore force both keys into one scope — a tenant supplying only its own TTS
key while riding the operator's Groq STT (or vice versa) would be impossible.
Two providers preserve ADR-0076's provider-mixing freedom under the unit rule,
at the cost of two credential cards in the Admin UI. This is also exactly the
shape ADR-0112 §3's own forward sketch anticipated.

**Why the third, config-only `voice` declaration.** `enabled` is
service-level, not per-half: `VOICE_ENABLED` gates STT and TTS together, and a
tenant that wants voice off wants *all of it* off. Declaring it on both halves
would invite conflicting rows; declaring it on one half arbitrarily would make
the other half's card lie about what it controls. A declaration with an empty
secret-field set is legal (the taxonomy documents `secret_fields` as "possibly
empty") and keeps policy flags explicit. The Admin surface renders it as a
third, toggle-only card.

**Field-set corrections to the §3 sketch, recorded deliberately.** The sketch
listed `base_url` and `voice` on both halves. Per ADR-0076 (and the shipped
adapter), TTS has intentionally **no** `base_url` knob and `voice` is
meaningless for STT — the declarations above are the precise field sets. The
sketch was explicitly aspirational; this amendment is where precision lands.

**What stays env-only.** `stt_provider` / `tts_provider` are not declared.
With exactly one supported adapter (`"openai"`, Groq reached via `base_url`),
a declared provider-key field would be dead weight validated only by the
factory's error branch. When a second adapter (ElevenLabs, Deepgram, …) lands,
its adapter ADR declares the field — the same discipline ADR-0112 §3 applies
to the future credentialed market-data adapters.

### 2. Policy: `env_fallback=True`, `optional=False`

`env_fallback=True` is non-negotiable: a single-tenant `.env` deployment must
keep working unchanged, served by the resolver's environment source (the
session-less resolver's env-only degradation is the same Stage-1 path every
other provider takes).

`optional=False` — deviating from the superficially attractive "absent
credential = voice quietly off". Absence-as-off would make a missing key a
*silent* fallback, colliding with the project-wide no-silent-fallback rule and
making a misconfiguration indistinguishable from a choice. Instead:

- "Voice off for this tenant" is expressed **exclusively** through the
  `voice.enabled` chain (tenant row → `VOICE_ENABLED` → default off).
- An **enabled-but-keyless** tenant is a configuration error and surfaces
  loudly at first use, with an actionable message following the
  `_NO_LLM_MESSAGE` pattern: name both fixable scopes (Admin → Providers &
  Credentials; `.env`), say nothing about restarting.

This mirrors `openrouter` exactly and preserves the loud-validation semantics
`VoiceConfig.__post_init__` provides today, relocated from process start to
first per-tenant use (today's validation is lazy-on-first-singleton-access
anyway, so the observable behaviour class is unchanged).

### 3. `ResolvedVoice` — the per-request value object

A frozen dataclass `ResolvedVoice` in `services/voice/` carrying the eight
runtime fields (`stt_provider`, `stt_model`, `stt_api_key`, `stt_base_url`,
`tts_provider`, `tts_model`, `tts_voice`, `tts_api_key` — **not** `enabled`,
which is gating, not runtime), with a **masked `repr`/`str`** after the
`ResolvedLLM` model: both keys render as `<set/unset; masked>` in any f-string,
log line or traceback. `VoiceConfig` cannot serve as the carrier — its default
dataclass `repr` would leak both keys.

`build_provider` accepts `VoiceConfig | ResolvedVoice` (the adapter reads
attributes only; the two shapes are structurally compatible on the eight
runtime fields). The provider instance is built **per request** — cheap by
construction, since the adapter already builds and closes its `AsyncOpenAI`
client per call.

### 4. Per-request resolution, one layer up, per surface

Resolution lives where LLM resolution lives — one layer above
`services/voice/`, per surface, through the one credential façade:

- **Web** (`web/routes/chat.py`): `POST /chat/voice` and `POST /chat/tts` each
  resolve a `ResolvedVoice` for the turn, inside the session's
  `tenant_context`, mirroring `_resolve_llm` / `_resolve_llm_through`
  (including the engine-less env-only degradation). The chain per field:
  credential — vault tenant → env; each config field — vault tenant → env →
  code default (the `VoiceConfig` defaults: `gpt-4o-mini-transcribe`,
  `https://api.openai.com/v1`, `gpt-4o-mini-tts`, `nova`).
- **Telegram** (`bot/telegram_bot.py`): the voice handler resolves per
  message inside the binding tenant's context, exactly as the F4 LLM turn
  does. Both the STT leg (inbound voice note) and the TTS leg (outbound voice
  reply) use the turn's single resolution.

Nothing is cached and nothing is stashed: the resolution lives for one call,
so one worker serves many tenants without their voice keys ever meeting —
binding decision D3 of ADR-0112 §4b, applied unchanged.

`services/voice/` itself stays DB-free and free of
`web`/`bot`/`core`/`modules` imports (ADR-0038 layering); the layering
regression guard is updated to construct via `ResolvedVoice` rather than the
retired singleton path.

### 5. Tenant-aware gating

`voice_enabled` ceases to be a process-global answer. The per-tenant answer is
`resolve_config("voice", "enabled", scopes=("tenant", "env"))`, parsed as bool
(`"true"`, case-insensitive — the `VOICE_ENABLED` convention), default off.
It is computed:

- **per render**, at both template-context sites that feed
  `_partials/shirley_section.html`'s `voice_enabled` flag (inside the
  session's tenant context the views already hold);
- **per turn**, in `POST /chat/voice` and `POST /chat/tts` themselves
  (defence in depth, as the global gate is today);
- **per voice message**, in the Telegram handler, inside the binding's tenant
  context.

Gating deliberately checks the `enabled` chain **only** — no credential
presence probe, which would put vault reads on every page render. The
enabled-but-keyless case is handled by §2's loud first-use error. The
unauthenticated-impossible invariant holds by construction: every gating site
runs inside an authenticated session or an authorised bot binding.

### 6. Singleton retirement

The module-level singletons `get_voice_provider` (factory) and
`get_voice_config` / `voice_enabled` (config) are **retired** as surface
APIs. The application scope is served by the session-less
`CredentialResolver`'s environment source; keeping a second, parallel
env-reading path alive would recreate exactly the double-precedence problem
ADR-0112 §4b eliminated for the LLM. `VoiceConfig` remains as the env-default
value object (its `default_factory` fields double as the code-default source
for §4's config chains and keep the existing config test suite meaningful);
its `__post_init__` validation continues to guard explicit construction.
Tests that monkeypatch the retired singletons
(`tests/bot/test_telegram_voice.py`, the web voice tests) are rewritten
against resolver doubles, following `test_bot_llm_resolution` /
`test_chat_llm_resolution`.

### 7. Admin UI: taxonomy-driven appearance only

No bespoke settings page. The three declarations appear on Admin → Providers
& Credentials through the existing taxonomy-driven rendering, plus entries in:

- `_PROVIDER_LABELS` — `voice` → "Voice", `voice_stt` → "Voice —
  speech-to-text", `voice_tts` → "Voice — text-to-speech";
- `_PROVIDER_DESCRIPTIONS` — one line each; public naming rules apply
  (Shirley may be named; internal agent names never);
- `_FIELD_LABELS` — `voice` → "Voice" (the persona-voice field; `api_key`,
  `model`, `base_url`, `enabled` already exist);
- `_FIELD_HINTS` — per-field one-liners (e.g. `voice_stt.base_url`:
  "OpenAI-compatible endpoint. Point at Groq for Groq STT.";
  `voice_tts.voice`: "The voice Shirley speaks with.");
- freshness pills — **"live — saves apply instantly"** for all three cards
  (§8).

### 8. Restart-free, completely

Per-request resolution makes every change instant: a tenant row written in
Admin answers the very next voice turn and the very next render of the voice
affordances. No Telegram-style restart caveat exists anywhere in this strand —
the pill copy above is the consumer-honesty statement of that fact.

### 9. Scheduling note (not a decision)

The strand may land before or after the AGPL public release (#052) without
architectural consequence. V1–V2 of the implementation strand (taxonomy,
value object) are purely additive; V3–V5 touch live surfaces. The actual
scheduling is recorded on roadmap item #059, not here.

## Alternatives considered

- **One `voice` provider with both keys.** Rejected: §1's unit-chaining rule
  would force both keys into one scope, destroying ADR-0076's provider-mixing
  freedom (Groq STT + OpenAI TTS with different owners). The UI saving (one
  card) does not pay for the capability loss.
- **`enabled` declared on both halves / on one half.** Rejected: two rows for
  one switch invite divergence; one arbitrary host makes the other card's
  scope dishonest. The config-only `voice` declaration costs one toggle card
  and states the truth.
- **`optional=True` (absent credential = voice quietly off).** Rejected as a
  silent fallback; see §2. Enablement and credential presence are separate
  questions with separate answers.
- **Gating with a credential presence probe.** Rejected for round 1: it puts
  vault reads on every page render to optimise for a misconfigured state that
  §2 already surfaces loudly and actionably at first use. May be revisited if
  operator experience shows the first-use error is reached often.
- **User-scope fields (per-user persona voice, per-user keys).** Deferred —
  not even declared model-side, deviating from `openrouter`'s v1 posture
  (§3/§7 of ADR-0112 declared the user scope UI-less). The §7 lesson was that
  model-carried scopes without UI create explanation burden; for voice there
  is additionally no pairing-style mechanism that would need the scope early.
  Adding `user` to a field's scope set later is a pure annex amendment.
- **A central `resolve_voice` helper in `services/investments/`.** Rejected:
  the F4 precedent resolves per surface (web, bot, Irene each own their
  helper), keeping each surface's degradation story local. Voice follows the
  precedent rather than introducing a new sharing pattern.

## Non-goals

- New voice providers/adapters (ElevenLabs, Deepgram, …) — the factory branch
  remains the extension point; their ADRs declare their taxonomy fields.
- User-scope voice credentials or persona voices (deferred, see above).
- Any change to the recording UX, the audio pipeline, streaming STT/TTS, or
  the Telegram opus/transmux follow-ups of ADR-0076.
- Declaring `stt_provider` / `tts_provider` in the taxonomy.
- Any bespoke voice settings page.

## Consequences

- `services/credential_vault/taxonomy.py` gains three declarations; the
  module docstring's "deliberately absent" note is updated to name only the
  future credentialed market-data adapters. `_ENV_CREDENTIAL_FIELDS` /
  `_ENV_CONFIG_FIELDS` gain the six env links; TX-01…TX-06 extend to cover
  them (TX-02/TX-06 pin the maps against the declarations by construction).
- `services/voice/` gains `ResolvedVoice`; `build_provider` widens its
  accepted input; the two module singletons are removed; the layering guard
  is updated. `VoiceConfig` stays.
- `web/routes/chat.py` and `bot/telegram_bot.py` gain per-request voice
  resolution and tenant-aware gating; their voice tests are rewritten against
  resolver doubles.
- Admin → Providers & Credentials shows three new cards with live pills; the
  presentational dicts gain entries.
- `.env.example` and the deployment docs gain a note that every `VOICE_*`
  variable is now the application-scope link of a taxonomy field (names
  unchanged — no migration for existing deployments).
- No schema change, no Alembic migration: `scoped_settings` carries the new
  rows under the existing vocabulary (provider/key validation is in code, per
  ADR-0112 §3's CHECK-constraint decision).
- Single-uvicorn-worker constraint: unaffected (noted for completeness — no
  new process-global state is introduced; state is in fact removed).

## Related ADRs

- **ADR-0076** — Voice I/O for Shirley (pipeline, protocol, adapter,
  per-call client lifecycle; configuration posture partially superseded here).
- **ADR-0112** — Scoped Settings & Credential Architecture (the base; §3
  amended by this annex; §1/§2/§4 applied unchanged).
- **ADR-0095** — Provider credential resolution contract (resolution order,
  masked logging; applied unchanged).
- **ADR-0038** — Layering rules (voice service purity preserved).
- **ADR-0115** — Watch Desk rename (public naming rules for UI strings).

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-08-12 | PortfoliFLOW project owner | Drafted (Proposed). |
| 2026-08-12 | PortfoliFLOW project owner | Accepted; index status updated. |
