# ADR-0076: Voice I/O for Shirley — Speech-to-Text and Text-to-Speech on the Web and Telegram Surfaces

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** PortfoliFLOW project owner
- **Tags:** ai-service, shirley, voice, stt, tts, web, telegram, multimodal, demo

---

## Context

Shirley's agentic tool-calls are the primary demo differentiator. The next
incremental capability is **spoken interaction** — the operator talks to
Shirley and hears her answer — without giving up any of the analytical
substance that already works in text. The strategic framing is unchanged:
voice is an *enhancement to the existing assistant*, not a new tool and not a
second assistant.

Reading the code confirms the single most important fact for this decision:
**Shirley already does the hard part — "mixed mode" — in text.** A voice
layer is therefore an adapter at the two edges of an unchanged turn pipeline,
not a re-architecture.

### The existing turn pipeline (unchanged by this ADR)

1. **Web.** `web/routes/chat.py::post_message` accepts text (and, since
   ADR-0075, optional images via multipart), appends a user `Message` to the
   session's in-memory `Conversation` (ADR-0050), stashes per-turn metadata
   in a bounded LRU, and returns the `partials/turn_started.html` fragment.
   That fragment carries a hidden SSE-bootstrap element with
   `data-pf-sse-url="/chat/stream/<turn_id>"`. `web/static/js/chat.js` opens a
   **native** `EventSource` (deliberately not the HTMX SSE extension — HTMX
   bug #2343) against `GET /chat/stream/<turn_id>`, which drives
   `AIServiceCore.stream_response()` and translates each `StreamEvent` into an
   SSE frame (`message`, `tool_called`, `tool_completed`, `chart`, `done`,
   `error`).

2. **Charts and prose are already separated in the DOM.** In
   `turn_started.html`, streamed prose lands in `#<assistant_bubble_id>`
   while `chat.js` appends Plotly charts to the sibling
   `#<assistant_bubble_id>-wrap`. The consequence is decisive for TTS: at the
   `done` event, `bubble.textContent` is **exactly the prose, with no chart
   payload mixed in** — nothing needs to be parsed or stripped to know what
   to speak.

3. **Telegram drives the same core.** `bot/telegram_bot.py` runs aiogram v3
   on its own daemon-thread asyncio loop, routes a single `@dp.message()`
   handler through the **same** `core.stream_response`, already accepts photo
   input (ADR-0075), already keeps per-chat conversation memory, and already
   *sends* charts outbound as PNG photos (`chart_artifact` →
   `BufferedInputFile` → `send_photo`). The bot's user-facing strings are
   German.

4. **The concurrency seam is `_TURN_LOCK`.** `stream_response` serialises
   every turn (across all consumers) under a process-global
   `threading.Lock`, acquired via `asyncio.to_thread` so the uvicorn loop
   stays responsive. STT and TTS, as defined below, sit **outside** this lock.

### Why this is not a generic "swap the provider" problem

Unlike LLM chat completions — where a strong de-facto OpenAI-compatible
standard (`/v1/chat/completions`) lets `AIServiceCore` point the same
`openai.AsyncOpenAI` client at OpenRouter, Groq, vLLM, etc. via a `base_url`
swap — audio is only **partially** standardised:

- **STT has a partial standard.** OpenAI's `/v1/audio/transcriptions`
  (multipart upload; `client.audio.transcriptions.create(...)`) is mirrored
  by Groq, so an OpenAI-compatible STT path covers OpenAI **and** Groq with a
  `base_url` swap.
- **TTS does not.** OpenAI's `/v1/audio/speech` is OpenAI-specific.
  ElevenLabs (different endpoint, `xi-api-key` header, its own streaming model
  and SDK), Deepgram, Azure, and Google each have their own wire format. A
  `base_url` swap does **not** carry across TTS providers.

Two further facts shape the design:

- **OpenRouter does not proxy audio.** Voice needs a *separate* provider
  integration with its own credentials, distinct from `OPENROUTER_API_KEY`.
- **No audio capability exists today.** A repository-wide search for
  `whisper`/`tts`/`stt`/`MediaRecorder`/`getUserMedia`/`deepgram`/
  `elevenlabs` returns zero hits. This is greenfield at the edges.

### Telegram: "voice call" is not available to a bot

A Telegram **bot cannot place or receive live calls.** Audio/video calls are
an MTProto *client-account* feature, not part of the Bot API; aiogram speaks
only the Bot API. (Userbot libraries such as pytgcalls + Pyrogram can join a
group voice chat as a logged-in account — a different auth model, fragile and
ToS-adjacent. Out of scope.)

What aiogram **does** support, and what maps exactly onto the turn-based
model, is **voice messages** (Sprachnachrichten, OGG/Opus). The Telegram
client provides the record-and-send UX, codec, microphone permission, and
mobile handling — so the Telegram path also **sidesteps the browser audio
quirks** (`MediaRecorder`, Safari, `getUserMedia`/HTTPS) for users who prefer
mobile or asynchronous interaction.

## Decision

PortfoliFLOW adds **turn-based voice input and output to Shirley** via a new,
**channel-agnostic** service `services/voice/`, consumed identically by the
web chat surface and the Telegram bot. STT is a **pre-processor** that turns
audio into the text that enters the existing pipeline; TTS is a
**post-processor** that turns the existing streamed prose into audio. The
Qt-free core (`services/ai_service_core.py`), the tool-execution loop, the
`ToolRegistry` brackets (ADR-0022), and the process-global `_TURN_LOCK` are
**not modified**. Charts, image input (ADR-0075), and multi-turn history
(ADR-0050) are reused as-is.

### Round-1 contract

- **Turn-based**, not conversational/full-duplex. On the web, the user records
  and presses an explicit "I'm done" control; on Telegram, sending the voice
  message *is* the end-of-turn signal.
- **Post-completion TTS.** The full assistant prose is synthesised **after**
  the turn's `done`/end-of-stream — no sentence-chunked streaming TTS in this
  iteration (recorded as a follow-up).
- **Voice is strictly additive.** Every text and image path continues to work
  unchanged; voice never becomes a hard dependency for a turn to succeed.

### The voice service (`services/voice/`)

A thin provider abstraction with two operations:

```
class VoiceProvider(Protocol):
    async def transcribe(self, audio: bytes, mime_type: str) -> str: ...
    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]: ...
        # returns (audio_bytes, output_mime_type)
```

- **OpenAI adapter (now).** Uses the `openai` SDK already present in the
  core — `client.audio.transcriptions.create(...)` and
  `client.audio.speech.create(...)`. **No new dependency.**
- **ElevenLabs-ready (later).** A second adapter file is all that is needed;
  the Protocol is the seam that keeps this cheap. Not built in this ADR.
- **STT `base_url` swap.** Because OpenAI-compatible STT exists, the STT
  adapter exposes an optional `base_url`, so Groq is a `.env` change, not a
  new adapter.
- **No silent fallback.** A misconfigured/unsupported provider, an empty
  transcript, or a synthesis failure surfaces a clear, human-readable message
  on the calling surface — the same discipline as
  `services/vision_capabilities.py` and `services/scraper/capabilities.py`.
  The service must not import from `web/` or `bot/` and must not import PyQt6
  (ADR-0038).

### Configuration (`.env`, both channels)

The voice service reads its own configuration from the environment (mirroring
`bot/config.py`'s `os.getenv` pattern and the self-configuring capability
helpers), so both surfaces consume one source of truth. STT and TTS are
configured **independently** so providers can be mixed (e.g. Groq-STT +
OpenAI-TTS, or OpenAI-STT + ElevenLabs-TTS):

```
VOICE_ENABLED=true
# Speech-to-text
VOICE_STT_PROVIDER=openai
VOICE_STT_MODEL=gpt-4o-mini-transcribe
VOICE_STT_API_KEY=...                       # may equal the TTS key for OpenAI
VOICE_STT_BASE_URL=https://api.openai.com/v1  # enables the Groq swap
# Text-to-speech
VOICE_TTS_PROVIDER=openai
VOICE_TTS_MODEL=gpt-4o-mini-tts
VOICE_TTS_VOICE=nova                        # Shirley's persona voice
VOICE_TTS_API_KEY=...
```

The keys are added to `.env.example`; `WebSettings` may surface them for
documentation, but the authoritative reader is `services/voice/`. When
`VOICE_ENABLED` is false (or required keys are absent), the surfaces hide the
voice affordances and behave exactly as today.

### Output format is per-channel (and is the main new risk)

`synthesize(..., fmt=...)` lets each adapter request the right container:

- **Web** requests `mp3` — universally playable in an `<audio>` element.
- **Telegram** requests `opus`. `send_voice` renders a playable voice note
  **only** for OGG-containered Opus; OpenAI TTS `response_format="opus"`
  returns Ogg Opus, which should satisfy it directly. **Risk:** if a provider
  returns a container Telegram rejects, the fallback is either an `ffmpeg`
  transmux or `send_audio` (renders as a file/audio player, not a voice note).
  This is the bot-side equivalent of the browser audio quirk and is the one
  genuinely new unknown.

### Web surface

- **STT in — `POST /chat/voice`** (multipart: audio + CSRF + optional
  `images[]`), parallel to `post_message`. It calls `transcribe()`, then
  performs **exactly** the work `post_message` does. To avoid duplication, the
  shared body of `post_message` (append user `Message`, trim, stash turn,
  render `turn_started.html`) is refactored into a private helper
  `_begin_turn(...)` that both endpoints call; the transcript becomes the
  turn's `text` and is shown in the user bubble (the user-direction
  transcript). The SSE-bootstrap element gains `data-pf-voice="1"` so
  `chat.js` knows the reply should be spoken.
- **TTS out — `POST /chat/tts`** (text → audio). On `done`, `chat.js` reads
  `bubble.textContent` (decoded prose; the wire is HTML-escaped, so
  `textContent` is correct) and, **only** when the turn was voice-initiated,
  POSTs it to `/chat/tts`, receives `mp3` bytes, and plays them. A prose-empty
  turn (chart-only) plays nothing.
- **Composer.** A voice-mode toggle reveals a record button. The browser uses
  `MediaRecorder` (`audio/webm;codecs=opus`; an `audio/mp4` fallback for
  Safari) and an explicit "I'm done" button to stop. `getUserMedia` requires
  HTTPS (Caddy already provides TLS; localhost is exempt). Microphone-denied,
  unsupported-`MediaRecorder`, and STT-failure cases fall back to the text
  composer with a clear inline message (reusing the `chat_error.html`
  pattern).

### Telegram surface

- **STT in.** A new `@dp.message(F.voice)` handler is registered **before**
  the existing catch-all. It downloads the OGG/Opus bytes
  (`aiobot.download(message.voice.file_id)`), calls the same
  `transcribe()`, and feeds the transcript into the existing message-handling
  core (the shared portion of `_handle_text_message`, refactored out so the
  voice and text entry points converge). Tool loop, conversation memory,
  vision, and chart-as-PNG output are all reused.
- **TTS out.** After the turn, the accumulated prose is passed to
  `synthesize(..., fmt="opus")` and sent via
  `aiobot.send_voice(BufferedInputFile(...))`. Charts continue to go as PNG
  photos; text chunks continue via `send_message` (so the transcript remains
  visible in-chat alongside the voice note).
- **Explicitly not calls.** No MTProto, no pytgcalls, no userbot. Voice
  messages only.
- **UX note.** A Telegram voice message cannot carry a photo in the same
  message; "comment on this chart by voice" is two messages on Telegram (or a
  photo with a text caption). The web composer keeps both in one turn.

### Scope

- **In scope:** turn-based STT input and post-completion TTS output on both
  surfaces; one channel-agnostic voice service; OpenAI provider; `.env`
  configuration; mixed mode (voice turn that also renders/ingests a chart or
  image) via the existing chart and ADR-0075 paths.
- **Out of scope (deliberate, recorded as follow-ups):** streaming /
  sentence-chunked TTS; streaming STT; realtime/voice-to-voice models; audio
  barge-in/interruption; EU data-residency provider wiring; persistence of
  audio; an in-app voice settings UI.

## Rationale

- **Minimal blast radius.** The high-risk core — the streaming loop,
  `_TURN_LOCK`, the `ToolRegistry` brackets — is untouched. The change is two
  edges (a pre-processor and a post-processor) plus one new service.
- **Mixed mode is free.** Chart generation and image-input evaluation already
  flow through the unchanged turn; the DOM already separates prose from
  charts, so TTS speaks the prose and the chart simply renders. This is the
  capability the demo wants, with no new plumbing.
- **The transcript is the chat history.** No separate transcript store is
  needed: STT result → user `Message`, prose → assistant `Message`, both
  render and rehydrate via the existing partials and `/chat/history`
  (ADR-0050). The "show a transcript during a voice exchange" requirement is
  satisfied by the surface that already exists.
- **Provider abstraction matches reality.** STT can ride a partial standard;
  TTS cannot — a Protocol with per-provider adapters is the honest shape and
  is exactly what keeps the system "ElevenLabs-ready" without building it now.
- **Channel-agnostic service = cheap Telegram path.** One service serves both
  the web SSE surface and the bot, mirroring the headless-core philosophy of
  ADR-0030 and ADR-0038. Because the bot already has the core, memory,
  vision, and chart-output plumbing, adding its voice path is small once the
  service exists.
- **No added lock contention.** STT runs in `POST /chat/voice` (before the
  turn) and TTS after `done` — both outside `_TURN_LOCK` — so voice does not
  extend lock-hold time or worsen the multi-tenant concurrency debt.

## Alternatives Considered

1. **OpenAI Realtime API / Gemini Live (full-duplex voice-to-voice).**
   Rejected for Round 1. It replaces Shirley's brain: the OpenRouter model
   choice, the tool loop, the `Soul_Shirley` system-prompt grounding, the
   chart artefacts, the multi-turn history, and the `_TURN_LOCK` gating would
   all be bypassed. It is a different architecture, not an enhancement, and it
   fragments the agent. Possible future "voice-native Shirley" track; not a
   quick win.
2. **Audio-native chat completions (`gpt-4o-audio-preview`).** Rejected. Folds
   STT into the LLM call and forces the audio model instead of the chosen
   Shirley model via OpenRouter, conflating two concerns the clean STT → text
   → tool-loop separation keeps apart.
3. **Browser Web Speech API (client-side STT+TTS, zero server cost).**
   Rejected as the shipped feature. Chrome-dependent, inconsistent TTS
   quality, and Chrome's STT still round-trips to Google — not demo-grade or
   provider-controllable. Acceptable only as a throwaway afternoon spike.
4. **Sentence-chunked streaming TTS in Round 1.** Deferred. Lower latency but
   requires a parallel audio channel, segment ordering, and buffering — a
   meaningfully larger track. The post-completion path is correct first.
5. **Telegram live calls via pytgcalls / a userbot.** Rejected. Not the Bot
   API; fragile; wrong auth model; ToS-adjacent. Voice messages achieve the
   turn-based goal natively.
6. **A single hard-coded OpenAI wire format for both STT and TTS.** Rejected.
   TTS providers diverge; a Protocol + adapter is required to remain
   ElevenLabs-ready and to honour the "no silent fallback" discipline.
7. **Routing voice through OpenRouter.** Not possible — OpenRouter does not
   proxy audio endpoints.
8. **Threading voice config through each surface's settings object.**
   Rejected in favour of the voice service owning its `.env` config, so STT
   and TTS are configured once for both channels (DRY; matches the
   self-configuring precedent of `vision_capabilities.py`).

## Consequences

### Positive

- A high-impact, low-risk demo capability: talk to Shirley and hear her
  reasoning, with charts and image analysis intact in the same exchange.
- Two delivery channels (browser and Telegram voice messages) from one
  service; the Telegram path doubles as a graceful sidestep of browser audio
  quirks.
- Voice hardens nothing and breaks nothing: the text and image paths are
  untouched and remain the fallback.

### Negative

- **Latency.** Post-completion TTS waits for the full LLM turn plus synthesis
  before any audio plays. Acceptable for short analytical answers; the
  streaming-TTS follow-up is the remedy if it grates.
- **Telegram container handshake.** The OGG/Opus `send_voice` requirement is a
  real integration risk with an `ffmpeg`/`send_audio` fallback.
- **Browser variance.** `MediaRecorder`/`getUserMedia` need HTTPS and a Safari
  fallback; some clients require explicit microphone permission.
- **Confidential audio.** Spoken portfolio discussion is potentially sensitive
  personal/commercial data. For demos this is mitigated by synthetic demo data;
  for production it is a deferred residency decision (below).
- **Marginal cost.** STT ≈ $0.004–0.006/min and TTS ≈ $15/1M chars (OpenAI) are
  negligible at demo scale; provider choice is driven by quality/latency/
  residency, not cost.

### Neutral / Follow-ups

- **Streaming TTS / streaming STT** for lower latency — separate, larger
  track.
- **EU data-residency** provider (e.g. Azure Speech) for production with real
  LP data — a documented pre-production seam; the Protocol already isolates
  the swap. Owner will decide before production.
- **Persona voice tuning** (steerable voices on `gpt-4o-mini-tts` or
  ElevenLabs) to match `Soul_Shirley`.
- **Voice settings under `/admin#ai-settings`** following the ADR-0052
  runtime-mutation pattern (env-only for now).
- **Barge-in / interruption** only becomes relevant if a future realtime track
  is pursued; not applicable to turn-based voice.
- The `_TURN_LOCK` process-global and the `resolve_tenant_id()` single-tenant
  seam are unchanged and out of scope here.

## Implementation Notes

Three blocks, one prompt each (per the project's roadmap-driven workflow);
the web blocks may ship before the Telegram block.

- **Block 1 — `services/voice/` (new).**
  - `services/voice/__init__.py`, a `VoiceProvider` Protocol, an
    `openai_provider.py` adapter (uses the existing `openai` SDK), a small
    `config.py` reading `VOICE_*` from the environment, and a
    `capabilities`-style guard for unsupported/empty results ("no silent
    fallback").
  - Layering: stdlib + `openai` only; must not import `web/` or `bot/` or
    PyQt6 (ADR-0038). Both surfaces import *from* it.
  - Tests: `tests/services/voice/test_openai_provider.py` (transcribe/
    synthesize happy paths with mocked HTTP via the project's `pytest-httpx`
    pattern; empty-transcript and synthesis-failure surfaced, not swallowed;
    `fmt` selects the container).
- **Block 2 — Web surface.**
  - `web/routes/chat.py`: refactor the shared turn-begin body into
    `_begin_turn(...)`; add `POST /chat/voice` (STT → `_begin_turn`) and
    `POST /chat/tts` (text → audio); pass `data-pf-voice` into
    `turn_started.html`.
  - `web/settings.py`: optionally surface `VOICE_*` for documentation.
  - `web/templates/_partials/shirley_section.html`: voice-mode toggle +
    record / "I'm done" controls.
  - `web/templates/partials/turn_started.html`: `data-pf-voice` on the SSE
    bootstrap element.
  - `web/static/js/chat.js`: `MediaRecorder` capture, POST to `/chat/voice`,
    and post-`done` TTS playback for voice-initiated turns; Safari fallback;
    permission/feature-detection error handling.
  - `web/static/css/components/chat.css`: voice control styling.
  - Tests: `tests/web/test_chat_voice.py` (multipart audio accepted →
    transcript becomes the user message → SSE opens; non-voice turns are not
    spoken; STT failure returns the inline error; `/chat/tts` returns audio
    bytes; voice disabled → endpoints/affordances absent).
- **Block 3 — Telegram surface (additive).**
  - `bot/telegram_bot.py`: `@dp.message(F.voice)` handler before the catch-all;
    download OGG/Opus; converge on the shared message core; `send_voice` with
    `fmt="opus"`; OGG/Opus container handling (with the `ffmpeg`/`send_audio`
    fallback noted).
  - `bot/config.py`: read the same `VOICE_*` keys if the bot does not consume
    the service's own config loader directly.
  - Tests: `tests/bot/test_telegram_voice.py` (inbound voice → transcript →
    turn; outbound prose → `send_voice`; unsupported container falls back;
    unauthorised sender still dropped; charts still sent as photos).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Functional Suitability (spoken
  interaction delivered without losing tools/charts/vision); Reliability (the
  "no silent fallback" guard prevents a misconfigured provider from failing
  silently; voice is strictly additive so a failure degrades to text);
  Maintainability (one shared, data-driven voice service rather than per-surface
  STT/TTS code); Portability (the Protocol isolates provider swaps, including a
  later EU-residency provider).
- **Regulatory references:** None directly engaged in this iteration. No tenant
  seam, trust class, or RLS posture changes (ADR-0022/0035/0047/0063
  unchanged). Audio is processed **in memory for the duration of one turn and
  not persisted**, mirroring the ADR-0075 image contract, which keeps the
  data-handling surface for potentially confidential spoken content minimal and
  auditable. Provider data-residency for production (relevant to the project's
  DORA-resilience-*designed* and MaRisk AT 7.2 framing — an obligation that
  rests with the financial entity, not a compliance claim by the tool) is a
  documented pre-production seam, not part of this decision.
- **Audit evidence:** `services/voice/` (the single, data-driven voice
  service and its capability guard); the STT-before-turn / TTS-after-turn
  placement that leaves `_TURN_LOCK` unchanged; the in-memory, non-persisted
  audio handling; the test suites listed above; this ADR.

## References

- Related ADRs: ADR-0030 (Telegram bot as first headless client — the
  channel-agnostic precedent this ADR extends), ADR-0038 (Qt-free core — the
  unchanged engine both voice surfaces drive), ADR-0048 (Shirley chart
  artefacts — the mixed-mode chart path reused verbatim), ADR-0050 (in-memory
  multi-turn history — the transcript surface and text-only rehydration this
  ADR relies on), ADR-0051 (Shirley embedded in Assistants — the web surface
  the voice composer lives in), ADR-0052 (AI settings runtime under Admin —
  the pattern a future voice settings UI would follow), ADR-0075 (multimodal
  image input — the direct sibling; voice reuses its vision path and its
  single-turn, in-memory, non-persisted data contract), ADR-0022 (tool trust
  classes — unchanged; no new tool is registered), ADR-0063 (per-turn
  tool-execution context — unchanged; voice turns carry it exactly as text
  turns do).
- Code referenced: `web/routes/chat.py`, `web/static/js/chat.js`,
  `web/templates/_partials/shirley_section.html`,
  `web/templates/partials/turn_started.html`,
  `services/ai_service_core.py` (`stream_response`, `_TURN_LOCK`),
  `services/ai_models.py` (`Conversation`, `Message`, `Attachment`),
  `services/vision_capabilities.py` and `services/scraper/capabilities.py`
  (the "no silent fallback" precedent), `bot/telegram_bot.py`,
  `bot/config.py`, `web/settings.py`, `docs/Soul_Shirley.md`.

---

## Revision History

| Date       | Author                     | Change                          |
|------------|----------------------------|---------------------------------|
| 2026-06-04 | PortfoliFLOW project owner | Initial draft, status Proposed. |
| 2026-06-04 | PortfoliFLOW project owner | Round 1 implemented across the web and Telegram surfaces (Blocks 1–3: services/voice provider + OpenAI adapter, POST /chat/voice & /chat/tts, Telegram F.voice handler with send_audio fallback). Status Accepted. |
