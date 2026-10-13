# ADR-0136: Voice Providers, Round 2 — Split Speech Ports, Provider Selection in the Taxonomy, Keyless and Local Adapters, an ElevenLabs Adapter, Telegram Transcoding

- **Status:** Accepted (2026-10-13)
- **Date:** 2026-10-13
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #076 — Voice providers, round 2
- **Supersedes / amends:** **annex amendment to ADR-0118** — §1 (the field
  sets of `voice_stt` / `voice_tts`, and the clause "`stt_provider` /
  `tts_provider` stay env-only until a second adapter's ADR declares them":
  this is that ADR), §2 (the `optional=False` policy and its "absent =
  quietly off is a silent fallback" argument), §3 (the `ResolvedVoice` value
  object), §4 (the per-surface resolution now shares one pure assembly
  function) and §5 (gating is per half). **Partially supersedes ADR-0076**:
  its "TTS does not [have a standard]" and "intentionally no TTS `base_url`"
  posture, its single two-method `VoiceProvider` Protocol, and its "stdlib +
  `openai` only" layering formula. It is the fifth annex amendment to
  ADR-0112 §3 (after ADR-0118, ADR-0123, ADR-0131, ADR-0132): three new
  config fields and one new field attribute. ADR-0076, ADR-0112 and ADR-0118
  themselves remain immutable and otherwise unchanged.
- **Tags:** voice, configuration, multi-tenancy, credentials, admin, telegram,
  shirley, providers, privacy

---

## Context

### What exists

ADR-0076 gave Shirley turn-based voice messages: speech-to-text before the
turn, text-to-speech after it, on the web chat and on Telegram, behind one
two-method Protocol `VoiceProvider` (`transcribe`, `synthesize`) with one
adapter, `OpenAIVoiceProvider`, serving both halves. ADR-0118 moved the
configuration into the scoped-settings taxonomy: three declarations
(`voice.enabled`, `voice_stt.{api_key, model, base_url}`,
`voice_tts.{api_key, model, voice}`), all `env_fallback=True` and
`optional=False`; per-request resolution on each surface into a frozen
`ResolvedVoice` of eight fields; provider keys left environment-only
("until a second adapter's ADR declares them"); gating on the `voice.enabled`
chain alone.

Verified against the 2026-10-12 image: `services/voice/` holds seven modules
(`provider.py`, `factory.py`, `openai_provider.py`, `resolved.py`,
`config.py`, `errors.py`, `__init__.py`); `factory.build_provider` accepts
`VoiceConfig | ResolvedVoice` and knows the one key `"openai"`; the TTS call
deliberately carries no `base_url`; the code defaults
`DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"` and
`DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"` are the final links of every config
chain; `VoiceConfig` has no consumer outside the package and its own test
module; `web/routes/chat.py` and `bot/telegram_bot.py` each carry a full copy
of the resolution (`_resolve_voice_through` / `_resolve_bot_voice_through`)
and of the user-facing message `_NO_VOICE_MESSAGE`; the Telegram reply path
sends `synthesize(..., fmt="opus")` bytes with `send_voice` and falls back to
`send_audio` on any exception, with no transcoding ("a documented Round-1
follow-up"); no reference to `ffmpeg` exists in code or installer.

### What changed outside

OpenAI has announced the removal of every model this code depends on
(developers.openai.com/api/docs/deprecations, checked 2026-10-12):

| Category | Shutdown | Models | PortfoliFLOW's code default |
|---|---|---|---|
| Text-to-speech | **2027-01-06** | `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts-2025-03-20`, `gpt-4o-mini-tts-2025-12-15` | `gpt-4o-mini-tts` |
| Transcription | **2027-02-26** | `whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `gpt-4o-transcribe-diarize` | `gpt-4o-mini-transcribe` |

The recommended TTS successor, `gpt-realtime-2.1-mini`, is a Realtime-API
model and does not serve `/v1/audio/speech`; there is no drop-in model swap.
A vendor decision of this kind must not be able to surprise the project
again.

### What the owner requires

- **No provider default.** A deployment with nothing configured must start,
  and voice is simply inactive — many prospective tenants hold no account
  with any speech provider, and the product must not force one on them.
- **Local operation.** A tenant may run speech-to-text and text-to-speech
  on its own server, in the spirit of the existing local-LLM posture, and a
  privacy-minded tenant may run one half locally and leave the other off.
- **A second, native adapter** for a cloud vendor (ElevenLabs), so that the
  same vendor account can later also serve Shirley's phone mode, which a
  separate ADR covers. Nothing call-specific belongs in the voice-message
  declarations.
- **Telegram voice replies arrive as real voice notes**, converted on the
  server when the provider's container is not what Telegram renders.
- Telegram keeps voice messages only; a phone mode exists only on the web.

### Constraints

The scoped-settings apparatus of ADR-0112 (unit-chained secrets,
individually chained config fields, the taxonomy as the single source of
policy, the taxonomy-driven Admin cards) is applied unchanged. The voice
package stays layering-clean (ADR-0038): no `web/`, `bot/`, `core/`,
`modules/` or database import. ADRs are immutable; this one amends by
reference. The application is not containerised (ADR-0124), so any system
tool it uses is a host package.

## Decision

### 1. Two ports, two builders

`services/voice/provider.py` declares two `runtime_checkable` Protocols in
place of the one:

```
class SpeechToText(Protocol):
    async def transcribe(self, audio: bytes, mime_type: str) -> str: ...

class TextToSpeech(Protocol):
    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]: ...
```

`services/voice/factory.py` exposes `build_stt(resolved: ResolvedSTT) ->
SpeechToText` and `build_tts(resolved: ResolvedTTS) -> TextToSpeech`. Each
builder knows the adapters of its half and raises
`UnsupportedVoiceProviderError` for an unknown key — no silent fallback. An
adapter class may implement both Protocols (the OpenAI-compatible one does);
the factory still composes one adapter per half, so a tenant may pair any
STT adapter with any TTS adapter.

`VoiceProvider` and `build_provider` **retire at once**, with no alias
release: every call site uses exactly one half, and the port split is a
behaviour-neutral refactor.

### 2. Adapters declare themselves in code

Each adapter carries a declaration the factory, the resolution (§5) and the
Admin card read:

| Key | Class | Requires API key | Defaults | Sunset |
|---|---|---|---|---|
| `openai_compatible` | OpenAI-wire adapter (`/v1/audio/transcriptions`, `/v1/audio/speech`) | **no** — local servers usually run unauthenticated; a cloud endpoint reached without a key fails loudly at first call | **none** — `model` and `base_url` are required for both halves, `voice` for TTS | — |
| `elevenlabs` | native adapter (§9) | **yes** | STT `model` `scribe_v2`; TTS `model` `eleven_multilingual_v2`; `base_url` `https://api.elevenlabs.io`; `voice` (the ElevenLabs voice ID) **required** | — |
| `openai` | the OpenAI-wire adapter under a legacy key | **yes** | `base_url` `https://api.openai.com/v1`; STT `model` `gpt-4o-mini-transcribe`; TTS `model` `gpt-4o-mini-tts`, `voice` `nova` — exactly today's code defaults | TTS 2027-01-06, STT 2027-02-26 |

The declaration is the single place a default lives. A cloud adapter knows
its vendor's current model; a wire-compatible adapter knows nothing about
the server behind the URL and therefore defaults nothing. **There is no
provider default**: the former `DEFAULT_VOICE_PROVIDER`, `DEFAULT_STT_MODEL`,
`DEFAULT_STT_BASE_URL`, `DEFAULT_TTS_MODEL`, `DEFAULT_TTS_VOICE`,
`env_stt_provider()`, `env_tts_provider()` and the env-default value object
`VoiceConfig` (with its `python-dotenv` import and its test module) retire.
The application scope is the resolver's environment source, as ADR-0118 §6
already argued; a second env-reading path has no reason to exist.

The `openai` key exists so that every `.env` and every tenant row written
under ADR-0076/ADR-0118 keeps working unchanged until OpenAI's own shutdown.
It is a declaration, not a code branch: the adapter class is the
OpenAI-compatible one, the defaults are OpenAI's, and the `sunset` dates are
data the Admin card hint and the operator handbook can show. Removing it
after the shutdown is the deletion of one declaration and one vocabulary
entry.

A further adapter (Deepgram, Azure, …) is a new declaration, a new factory
branch and a vocabulary entry; Mistral's transcription endpoint reproduces
the OpenAI wire (`https://api.mistral.ai/v1` + `audio/transcriptions`,
documented with the OpenAI SDK; docs.mistral.ai, checked 2026-10-12) and is
therefore a documented `openai_compatible` use, not an adapter.

### 3. Provider selection and the TTS endpoint enter the taxonomy

Three new config fields and one new field attribute — the fifth annex
amendment to ADR-0112 §3:

| Provider | Field (kind) | Scopes | Env link (`_ENV_CONFIG_FIELDS`) |
|---|---|---|---|
| `voice_stt` | `provider` (config, **choices**) | tenant | `VOICE_STT_PROVIDER` |
| `voice_tts` | `provider` (config, **choices**) | tenant | `VOICE_TTS_PROVIDER` |
| `voice_tts` | `base_url` (config) | tenant | `VOICE_TTS_BASE_URL` |

`base_url` now means "the provider's endpoint", whatever the adapter: the
OpenAI wire's root for `openai_compatible`, the API host for `elevenlabs`
(and its EU-residency host, §9). ADR-0076's "no TTS `base_url`" rested on
TTS having no standard; local servers (Speaches, Kokoro-FastAPI, vLLM-Omni)
reproduce `/v1/audio/speech` today, so the premise no longer holds.

`ProviderField` gains an optional `choices: frozenset[str] | None`. A field
with `choices` is rendered by the taxonomy-driven Admin card as a `<select>`
instead of a text input, and the card's write path rejects any value outside
the set with the usual inline error — a generic mechanism, not a voice
special case. The vocabulary (`openai_compatible`, `elevenlabs`, `openai`) is
owned by the adapter declarations in `services/voice/`; the taxonomy
**mirrors it as a literal** and a contract test pins the two sets equal,
because the taxonomy must not import the voice package (which would pull
`openai` and `httpx` into its import graph) and the voice package must not
import the taxonomy (ADR-0038).

All three voice declarations become **`optional=True`**. The static flag
cannot express "a key is required when the adapter says so"; it made the
resolver raise before any adapter was consulted, which would make a keyless
local adapter impossible. The "key required?" question moves into the voice
assembly (§5), driven by the adapter's declaration. This changes the
*mechanism* of ADR-0118 §2, not its intent: a misconfiguration is still loud
— on the Admin card and at first use.

Both provider keys were deliberately undeclared in ADR-0118 §1 pending "a
second adapter's ADR". This is that ADR.

### 4. Availability per half; completeness loud

For each half, independently:

- **Active** iff `voice.enabled` resolves true **and** that half's
  `provider` resolves to a non-empty value. Both are config reads along the
  tenant → env chain; **no credential probe**, exactly as ADR-0118 §5.
- **Inactive** otherwise: the affordance is absent and nothing is resolved.
  This is a deliberate state (no provider chosen), not a missing credential,
  which is why it may be quiet. ADR-0118 §2 rejected "absent credential =
  quietly off"; that rejection stands — an absent credential for an adapter
  that requires one is a misconfiguration and remains loud. What may be
  quiet is the absence of a *choice*.
- **Misconfigured**: a half is active but its resolved configuration is
  incomplete — a required `model`, `base_url` or `voice` is empty, the
  adapter requires an API key and none resolves, or the provider key is
  unknown. Resolution raises `VoiceConfigurationError` with a message naming
  the field and both scopes an operator can fix it in. The web endpoints
  answer 503, the Telegram bot replies with the message; **the program never
  fails to start.**

Consequences per surface:

- **Web.** The microphone toggle and the recording controls render iff STT
  is active; the template receives `voice_stt_enabled` and
  `voice_tts_enabled` in place of `voice_enabled`. A voice-initiated turn is
  spoken iff TTS is active (the SSE bootstrap's voice marker is set only
  then, so the client does not request speech it cannot get). A spoken
  question answered in text is a supported outcome, as a chart-only turn
  already is. `POST /chat/voice` 404s when STT is inactive; `POST /chat/tts`
  404s when TTS is inactive.
- **Telegram.** An inbound voice message is transcribed iff STT is active
  (else the existing "not enabled" reply); the reply is additionally sent as
  a voice note iff TTS is active, else as text only.
- **A TTS-only configuration is unreachable on both surfaces**: replies are
  spoken only to spoken questions. The ADR states this plainly rather than
  hiding it; the Admin card says so in its state line; a "read aloud"
  affordance that would make TTS-only reachable is a follow-up, not part of
  this decision.
- **Admin card.** The `Voice` card renders a computed state line beneath its
  description, after the Telegram card's precedent, e.g. *"on — speech-to-
  text: active (elevenlabs) · text-to-speech: inactive, no provider
  selected"* or *"off"*; a misconfigured half shows its message. It is
  computed from the same config chains the surfaces read (three config
  reads, no cipher). The freshness pill stays "live — saves apply instantly".

### 5. One pure assembly, I/O per surface

ADR-0118 rejected a central resolver helper in `services/investments/` and
kept resolution per surface; that stands for the **I/O**. The **rules** —
availability, completeness, keyless adapters, defaults, the legacy key, the
message text — now live once, in a pure function of `services/voice/`:

```
assemble_stt(enabled: bool, provider: str | None, model: str | None,
             base_url: str | None, api_key: str | None) -> ResolvedSTT | None
assemble_tts(enabled: bool, provider: str | None, model: str | None,
             base_url: str | None, voice: str | None, api_key: str | None) -> ResolvedTTS | None
```

`None` is *inactive*; a value object is *active*; `VoiceConfigurationError`
is *misconfigured*. The functions read no environment, no database and no
clock; they apply the adapter declaration (§2) to already-resolved raw
values. Each surface keeps its resolver calls (inside its tenant context,
with its engine-less degradation) and hands the raw values over. The two
copies of `_NO_VOICE_MESSAGE` collapse into the message the function raises.

The surfaces resolve only the half an operation needs: `POST /chat/voice`
and the inbound Telegram leg resolve STT, `POST /chat/tts` and the outbound
Telegram leg resolve TTS. ADR-0118's "both halves resolve on every call"
existed to make one-key-of-two loud; under per-half availability one half
configured and the other absent is a legitimate state, and an incomplete
half is loud by §4 on its own.

### 6. Two value objects

`ResolvedVoice` is replaced by two frozen dataclasses in
`services/voice/resolved.py`, mirroring the two ports and the two taxonomy
providers:

```
ResolvedSTT(provider, model, api_key, base_url)
ResolvedTTS(provider, model, voice, api_key, base_url)
```

`api_key` may legitimately be the empty string for a keyless adapter; the
masked `__repr__`/`__str__` (`<set/unset; masked>`) is unchanged. One object
for both halves would need a sentinel to express "this half is inactive",
and a sentinel inside a value object is exactly the quiet state §4 forbids.

### 7. Output formats and Telegram transcoding

`synthesize(text, *, fmt)` keeps the vocabulary `"mp3"` | `"opus"`. `"mp3"`
is what the web `<audio>` element plays. `"opus"` means *Opus audio in
whatever container the adapter delivers*; whether that container is Ogg is
decided at runtime, not assumed per vendor:

1. The Telegram reply path inspects the first bytes. `OggS` (the Ogg page
   capture pattern) → `send_voice` directly, as today.
2. Otherwise, if `shutil.which("ffmpeg")` finds a binary, a new stdlib-only
   module `services/voice/transcode.py` runs it as a subprocess over pipes
   (`-i pipe:0 … -c:a libopus -f ogg pipe:1`) — no temporary file, the audio
   stays in memory for the one turn (ADR-0076's data contract) — and the
   result goes to `send_voice`.
3. Without `ffmpeg`, the existing `send_audio` fallback applies, with the
   bytes as delivered.

`ffmpeg` is a **host system package, optional**: `scripts/install.sh`
reports it in the preflight and in `--doctor` as present or "optional, not
found — Telegram voice replies may arrive as audio files", never as a
failure. The operator handbook names the `apt-get` / `brew` install line.

### 8. Layering formula

The package rule becomes: **`services/voice/` imports only the standard
library, `openai` and `httpx`.** `httpx` is already a direct dependency
(`pyproject.toml`) and is in the import graph through `openai` in any case;
the ElevenLabs adapter uses it directly — **no `elevenlabs` SDK**, which
would add a dependency for two HTTP calls. The package docstrings
(`__init__.py`, `config.py`'s successor, `errors.py`, `resolved.py`) state
the same formula; the fresh-subprocess layering guard keeps its shape and
constructs every adapter of both halves from literal `ResolvedSTT` /
`ResolvedTTS` values. The forbidden-module list is unchanged.

### 9. The ElevenLabs adapter

Facts verified on ElevenLabs' public documentation on 2026-10-12
(elevenlabs.io/docs/api-reference/speech-to-text/convert,
…/text-to-speech/convert, …/overview/models,
…/product-guides/administration/data-residency):

- **Authentication:** header `xi-api-key`, both endpoints.
- **STT:** `POST {base_url}/v1/speech-to-text`, multipart form: `file`
  (the recorded audio; "all major audio and video formats are supported"),
  `model_id` (required; the adapter default is `scribe_v2`, the current
  non-deprecated model; `scribe_v1` is deprecated). The adapter sends
  `tag_audio_events=false` — the documented default `true` would put
  "(laughter)"-style tags into Shirley's transcript — and no
  `language_code`, so the language is detected (German is among the 90+
  languages). The transcript is the response's top-level `text`. `diarize`
  stays at its default `false`.
- **TTS:** `POST {base_url}/v1/text-to-speech/{voice_id}` with JSON `text`
  and `model_id` (documented API default `eleven_multilingual_v2`, which
  lists German; `eleven_flash_v2_5`, `eleven_v3` and `eleven_v4` also do),
  query `output_format`. The adapter maps `"mp3"` → `mp3_44100_128` (the
  documented default; `mp3_44100_192` would need the Creator tier) and
  `"opus"` → `opus_48000_64` (the `opus_48000_*` values carry no tier
  note). Whether the Opus response is Ogg-containered is not documented;
  §7's byte check settles it at runtime, and the implementation walk
  records the observed answer.
- **`voice` is the ElevenLabs voice ID**, not a name; the Admin hint says
  so. No default voice is shipped — a voice ID in code would be a guess.
- **EU data residency:** base URL `https://api.eu.residency.elevenlabs.io`,
  an isolated workspace with its own sign-in and API key, available to
  **Enterprise** customers only. An operator sets it as `base_url`; nothing
  else changes.

The adapter raises `VoiceTranscriptionError` / `VoiceSynthesisError` on any
non-2xx or transport failure and `EmptyTranscriptError` on an empty
transcript, like the OpenAI-compatible one. It is built per request with a
per-call `httpx.AsyncClient`, the lifecycle discipline ADR-0076 set.

One ElevenLabs account may later also serve Shirley's phone mode. The
`voice_*` declarations therefore carry nothing call-specific; a phone-mode
ADR adds its own declaration and may document reusing the same key.

### 10. Existing deployments and the deadlines

- A deployment whose `.env` or tenant rows name `openai` as provider (the
  `.env.example` of ADR-0076 and ADR-0118 wrote `VOICE_STT_PROVIDER=openai`
  and `VOICE_TTS_PROVIDER=openai` explicitly) behaves exactly as before
  through the legacy declaration (§2) — models, voice and the OpenAI
  endpoint default as today.
- A deployment that **deleted** `VOICE_*_PROVIDER` and relied on the former
  code default is **inactive** after the upgrade: the Admin card shows
  "inactive, no provider selected", the CHANGELOG names the change, and
  selecting `openai` (or any provider) restores it. This is the one
  behaviour change for existing installations and it is visible, not
  silent.
- After 2027-01-06 (TTS) and 2027-02-26 (STT) OpenAI rejects the legacy
  models at the provider: the web answers 502 "Voice reply unavailable" on
  speech and the inline STT error on transcription, the bot skips the voice
  note (text already sent) or replies with the STT error — loud at use, as
  the no-silent-fallback rule requires. The legacy declaration is removed in
  a follow-up change after the shutdown.
- `.env.example` and the operator handbook name both dates and show one
  cloud example (`elevenlabs`) and one local example (`openai_compatible`
  against a local server).

### 11. Local engines — operator guidance, not a dependency

The `openai_compatible` adapter is meant to reach, among others
(documentation checked 2026-10-12):

- **STT:** Speaches (MIT; faster-whisper behind the OpenAI wire), vLLM.
- **TTS:** Speaches (Piper, Kokoro), Kokoro-FastAPI (Apache-2.0;
  `/v1/audio/speech` with `mp3`, `opus`, `wav`, `flac`, `aac`, `pcm`),
  vLLM-Omni (`POST /v1/audio/speech`, OpenAI-compatible; Qwen3-TTS, Voxtral
  TTS and others; `response_format` `wav`, `mp3`, `flac`, `pcm`, `opus`).

Licence caveats the operator guide must carry, because "open weights" is
not "free for commercial on-premise use":

| Engine / weights | Licence | German |
|---|---|---|
| Voxtral TTS (`voxtral-mini-tts-2603`) | **CC BY-NC 4.0** (Mistral models table) — not for commercial on-premise use without a licence | yes |
| XTTS-v2 | **Coqui Public Model License** (model card) — non-commercial | yes |
| F5-TTS checkpoints | **CC-BY-NC-4.0** (model card) | not stated |
| Kokoro-82M | Apache-2.0 | **no** — en-US/GB, ja, zh, es, fr, hi, it, pt-BR only (VOICES.md) |
| Voxtral Small / Mini Transcribe Realtime (STT) | Apache 2.0 | yes |

PortfoliFLOW ships none of these and takes no dependency on any of them; the
deployment document of the implementation strand carries this table.

## Rationale

- **Two ports match the two taxonomy providers and the two surfaces' call
  sites.** Every caller uses one half; a composite Protocol forced one
  adapter class to serve both and made "pair local STT with cloud TTS"
  impossible without a wrapper.
- **No provider default is the owner's requirement, and the adapter
  declaration is the smallest honest home for the defaults that remain.** A
  wire-compatible adapter cannot know the server's models; a vendor adapter
  can. Putting defaults where the knowledge is removes five code constants
  and the parallel env reader ADR-0118 §6 already called a double-precedence
  risk.
- **Quiet "no provider" is not a silent fallback.** A fallback substitutes
  one behaviour for another without telling anyone; "inactive, shown on the
  Admin card" is a state with a name. Everything downstream of the choice
  stays loud.
- **A pure assembly function shares rules without sharing I/O.** It keeps
  ADR-0118's per-surface degradation story and the bot's "no `web/`
  import" rule, while ending the duplication of the rules and the message.
- **Runtime container detection beats per-vendor assumptions.** Four bytes
  tell the truth about every adapter, present and future; `ffmpeg` over
  pipes keeps the in-memory data contract; the fallback keeps the reply
  reaching the user.
- **`httpx` over an SDK.** Two HTTP calls do not justify a vendor SDK and
  its transitive surface; the layering guard stays as tight as it is.
- **`choices` on the field is a taxonomy feature, not a voice feature.** It
  makes a typo impossible at the one place the owner types, with one
  attribute and no bespoke UI.

## Alternatives Considered

- **Keep one `VoiceProvider` Protocol and one adapter per vendor.**
  Rejected: forbids mixing a local STT with a cloud TTS unless every vendor
  adapter also wraps the OpenAI wire; the two surfaces never use both
  methods at one site.
- **Keep `VoiceProvider` as a composite alias for one release.** Rejected:
  nothing would consume it; an alias that exists to be deleted is noise.
- **Keep a provider default (`openai_compatible`, or `openai` until the
  shutdown).** Rejected by the owner: it forces every tenant to hold a
  speech-provider account, and after the shutdown it would default to a dead
  endpoint.
- **`optional=False` kept, keyless adapters expressed as "empty key
  tolerated".** Rejected: the resolver raises before any adapter is asked;
  the flag is static per declaration and cannot depend on the chosen
  adapter.
- **Availability only when both halves are configured (today's rule).**
  Rejected: a privacy-minded tenant can run good local STT while German
  local TTS is still weak, and a spoken question answered in text is
  already a supported outcome. The price — TTS-only is unreachable — is
  stated in §4.
- **A credential presence probe for gating.** Rejected again (ADR-0118
  §5): vault reads on every render to optimise a state §4 surfaces loudly.
- **One `ResolvedVoice` with a ninth field `tts_base_url`.** Rejected: an
  inactive half would need a sentinel value inside the object.
- **A central `resolve_voice` in `services/investments/`.** Rejected, as in
  ADR-0118; the pure function in `services/voice/` shares the rules
  without taking the I/O.
- **PyAV instead of the host `ffmpeg`.** Rejected: a new binary dependency
  whose PyPI wheels bundle an FFmpeg build of unstated licence
  configuration, where the owner's requirement names the host `ffmpeg` and
  the fallback already exists.
- **The `elevenlabs` Python SDK.** Rejected: a dependency for two HTTP
  calls; it would also widen the layering formula beyond `httpx`.
- **Free-text `provider` input with a hint.** Rejected in favour of
  `choices`: a typo would be stored and surface only at first use.
- **Streaming STT/TTS, sentence-chunked playback.** Out of scope; see
  Follow-ups.

## Non-goals

- Shirley's phone mode, a call port, a conversation model, any client tool
  for calls — a separate ADR. Only §9's "nothing call-specific in the
  `voice_*` declarations" is kept here.
- Streaming STT or TTS, sentence-chunked playback.
- Native adapters beyond ElevenLabs (Deepgram, Azure, Mistral): "a further
  adapter is a new declaration, a new factory branch and a vocabulary
  entry".
- Hosting local models inside PortfoliFLOW; §11 is operator guidance.
- A "read aloud" affordance for typed turns.
- User-scope voice fields (still deferred, as in ADR-0118).

## Consequences

### Positive

- Voice messages run on any OpenAI-wire server, local or cloud, on
  ElevenLabs, and on OpenAI until its shutdown — chosen per tenant, per
  half, without a restart.
- A deployment with nothing configured starts and shows why voice is
  inactive.
- One place holds the rules (assembly), one place holds each default
  (adapter declaration), one place holds the vocabulary (declarations,
  mirrored and pinned).
- Telegram voice replies render as voice notes whatever the adapter's
  container, where `ffmpeg` is present.

### Negative

- Three config reads per render instead of one for the voice affordances
  (enabled, STT provider, TTS provider) — plain rows, no cipher; judged
  negligible on an authenticated page render.
- `ffmpeg` becomes an optional host package the operator must install for
  the best Telegram experience; the installer reports, it does not install.
- A TTS-only configuration is a dead end on both surfaces until a read-aloud
  affordance exists.
- An installation that relied on the former code default `openai` must
  select a provider after the upgrade.
- The legacy `openai` declaration must be removed after 2027-02-26 — a
  scheduled deletion, recorded on the roadmap item.

### Neutral / Follow-ups

- Remove the legacy `openai` declaration after OpenAI's shutdown.
- Streaming STT/TTS and sentence-chunked playback (ADR-0076's follow-up,
  still open).
- A "read aloud" affordance for typed turns, which would make TTS-only
  configurations reachable.
- Native adapters beyond ElevenLabs as demand arises.
- Record in the implementation walk whether ElevenLabs' `opus_48000_64`
  arrives Ogg-containered.

## Implementation Notes

- `services/voice/`: `provider.py` (two Protocols), `factory.py`
  (`build_stt`, `build_tts`, adapter registry), `openai_provider.py` (the
  OpenAI-wire adapter, TTS with `base_url`), new `elevenlabs_provider.py`,
  `resolved.py` (`ResolvedSTT`, `ResolvedTTS`), new `assembly.py`
  (`assemble_stt`, `assemble_tts`), new `transcode.py`, `errors.py`
  (unchanged hierarchy; `VoiceConfigurationError` is the misconfiguration
  error), `__init__.py` (exports; `config.py` and the `DEFAULT_*`
  constants, `env_*_provider()` and `VoiceConfig` removed).
- `services/credential_vault/taxonomy.py`: `ProviderField.choices`; the
  three new fields; `optional=True` on the three voice declarations; the
  mirrored vocabulary literal.
- `services/investments/credential_resolver.py`: three `_ENV_CONFIG_FIELDS`
  entries.
- `web/routes/chat.py`, `web/templates/_partials/shirley_dock.html`,
  `web/templates/_partials/turn_started.html`, `web/static/js/chat.js`:
  per-half flags, half-specific resolution, assembly call.
- `bot/telegram_bot.py`: half-specific resolution, assembly call, Ogg check
  and transcode before `send_voice`.
- `web/routes/provider_credentials.py` and
  `web/templates/_partials/provider_credentials_section.html`: `<select>`
  for `choices`, value check in the write path, labels/hints for
  `provider` and `voice_tts.base_url`, the computed state line on the
  `Voice` card.
- `scripts/install.sh`: optional `ffmpeg` in preflight and `--doctor`.
- `.env.example`, `docs/operator-handbook.md` §8, `CHANGELOG.md`
  (Unreleased), the deployment document for local engines.
- Tests: `tests/services/voice/` (factory, both adapters, resolved objects,
  assembly, transcode, layering guard), `tests/web/test_chat_voice*.py`,
  `tests/bot/test_*voice*.py`, `tests/services/credential_vault/
  test_taxonomy.py` (voice declarations, `choices`, vocabulary contract),
  `tests/services/investments/test_credential_resolver_config.py`,
  `tests/web/test_provider_credentials.py`.

## Compliance & Audit Relevance

- **ISO 25010:** Portability (provider and deployment independence; a
  vendor shutdown is a configuration change, not a release), Reliability
  (no silent fallback preserved; every failure surfaces at a named place),
  Security (keys stay per tenant, resolved per request, masked in every
  rendering; keyless local operation keeps audio on the tenant's own
  server), Maintainability (one rule set, one default per adapter, one
  vocabulary).
- **Regulatory references:** none newly engaged. Audio remains in memory
  for one turn and is never persisted (ADR-0076); the transcoding subprocess
  works over pipes. Data residency (an obligation of the financial entity
  under its DORA/MaRisk framing, not a claim by the tool) gains two concrete
  options: a tenant-local server, or ElevenLabs' EU-residency host.
- **Audit evidence:** the adapter declarations and the vocabulary contract
  test; the pure assembly function and its tests (inactive / active /
  misconfigured); the layering guard; the Ogg check and transcode tests; the
  Admin card state line; this ADR.

## References

- Related ADRs: ADR-0076 (voice I/O; partially superseded here), ADR-0112
  (scoped settings; §3 amended), ADR-0118 (voice in the taxonomy; amended),
  ADR-0038 (layering), ADR-0095 (credential resolution contract), ADR-0124
  (installer), ADR-0131 (config-only declaration precedent).
- OpenAI deprecations — developers.openai.com/api/docs/deprecations
  (2026-10-12).
- ElevenLabs — Speech to Text and Text to Speech API references, Models
  overview, Data residency guide (2026-10-12).
- Mistral — Offline transcription (2026-10-12); Models overview (licences).
- Speaches, Kokoro-FastAPI, vLLM-Omni speech API documentation; Hugging
  Face model cards for Kokoro-82M (VOICES.md), XTTS-v2, F5-TTS (2026-10-12).

---

## Revision History

| Date       | Author                     | Change                          |
|------------|----------------------------|---------------------------------|
| 2026-10-13 | PortfoliFLOW project owner | Initial draft, status Proposed. |
| 2026-10-13 | PortfoliFLOW project owner | Status Accepted.                |
