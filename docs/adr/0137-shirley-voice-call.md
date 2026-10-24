# ADR-0137: Shirley Voice Call — a Vendor-Hosted Conversation Layer with One Read-Only Client Tool into Shirley

- **Status:** Accepted (2026-10-14)
- **Date:** 2026-10-14
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #077 — Shirley voice call
- **Supersedes / amends:** none superseded. **Sixth annex amendment to
  ADR-0112 §3** (after ADR-0118, ADR-0123, ADR-0131, ADR-0132, ADR-0136): one
  new config-only-plus-secret declaration, `voice_call`. **Amends by
  reference:** ADR-0076 (its "voice-native Shirley" track, named in its
  Alternatives, is taken up here as a *second* conversation layer beside the
  turn-based one, not as a replacement); ADR-0118 / ADR-0136 (the `voice_*`
  declarations stay free of anything call-specific, as ADR-0136 §9
  announced; the persona voice `voice_tts.voice` is read as a default);
  ADR-0031 (the process-global turn lock is kept; this ADR only records how
  a call behaves behind it and names roadmap #071 as the fix); ADR-0050 (the
  in-memory history gains one optional attribute and one new writer — the
  call); ADR-0022 (the per-turn tool trust gating is kept; a call turn adds
  an explicit allowlist on top). All amended ADRs remain immutable and
  otherwise unchanged.
- **Tags:** voice, shirley, ai-service, web, configuration, multi-tenancy,
  credentials, admin, providers, privacy, security

---

## Context

### What exists

- **Voice messages** (ADR-0076, ADR-0118, ADR-0136): a recorded question is
  transcribed, answered by Shirley's ordinary tool-calling turn and spoken
  back after the turn ends. Speech-to-text and text-to-speech are two ports
  (`services/voice/`), chosen per tenant and per half, with a native
  ElevenLabs adapter among three. Nothing in that service is call-specific.
- **Shirley's turn** is `AIServiceCore.stream_response` (ADR-0038): one
  asynchronous generator yielding `chunk`, `tool_called`, `tool_completed`,
  `chart_artifact`, `stream_finished` and `error`, with the per-turn tool
  gating of ADR-0022 bracketed by `ToolRegistry.begin_turn()` /
  `end_turn()`. The tools come from `ToolRegistry.get_tool_definitions()`;
  the method has no parameter to narrow them. Sixteen tools are registered
  today — portfolio overview, statistics, limit coverage, the SAA
  comparison and configuration, investment lists and details, NAV history
  and data, dataset listing and slices, analysis results, two chart tools,
  web research — and every one of them is a read: nothing under
  `services/tools/` adds, commits, inserts, updates or deletes.
- **The turn lock** (ADR-0031, relocated by ADR-0038): `_TURN_LOCK`, a
  module-level `threading.Lock` in `services/ai_service_core.py`, acquired
  via `asyncio.to_thread` and released in a `finally`, serialises *every*
  user's turns process-wide. A tool runs inside `asyncio.to_thread` and
  cannot be interrupted; closing the generator takes effect at its next
  `await`. Roadmap #071 is the open fix and "needs an ADR".
- **The web surface**: `POST /chat/messages` allocates a turn and returns the
  fragment that opens a native `EventSource` against
  `GET /chat/stream/{turn_id}` (ADR-0050 history in `app.state.chat_histories`,
  trimmed at 20 messages / 24,000 characters; chart artefacts in a sidecar,
  ADR-0114). `GET /chat/dock` renders the one chat instance, which
  `shirley.js` *moves* between the 380 px dock column and the 420 px stage;
  the dock's voice controls gate on a per-tenant flag. Every write endpoint
  carries `require_session`, `require_role("owner", "member")` and
  `verify_csrf`; the regression guard `test_web_no_env_reads.py` keeps
  `web/` free of environment reads. `Message` has no attribute saying which
  channel produced it.
- **Third-party JavaScript is vendored** under `web/static/vendor/<lib>-<version>/`
  (htmx, Plotly, Tabulator; ADR-0037's vendor posture). No page loads a CDN.
  The web application sets no Content-Security-Policy header; the
  deployment's reverse proxy may.
- **Configuration** is the scoped-settings taxonomy (ADR-0112): a provider
  declaration with secret and config fields, `env_fallback`, `optional`;
  `provider_channel` (ADR-0131) is the precedent for a per-tenant switch
  that deliberately has *no* environment link; boolean config fields render
  as switches (ADR-0136 §3); the Admin card is taxonomy-driven.
- **The website** announces a phone mode for Shirley. Today only voice
  messages exist.

### What the owner requires

- A fluid spoken conversation with Shirley — interruption, thinking pauses,
  a natural feel — accepted as a *second* way of talking to her, beside the
  turn-based voice messages that stay provider-agnostic and may run locally.
- The conversation layer is vendor-hosted in v1 and separate from Shirley:
  it carries the persona, not the program functions; Shirley stays the one
  brain of record for every number, name, date and position.
- One vendor is fine for v1 (ElevenLabs), but lock-in is avoided in
  principle: a second vendor, or a self-hosted layer later, must fit behind
  a seam without a surface change.
- Calls are read-only; the layer may say "I can't do that here, please use
  the UI".
- No provider default; nothing configured means the feature is inactive and
  the program starts. The call is its own per-tenant opt-in. No
  deployment-wide switch for a feature that ships audio to a vendor.
- PortfoliFLOW provisions and updates the vendor agent through the vendor
  API; the agent definition lives in the repository.
- Telegram keeps voice messages only; the call is web-only.
- Shirley sounds the same in both modes only when both use the same vendor;
  a difference is accepted otherwise.

### What was verified

**Vendor facts** (ElevenLabs public documentation and API reference, read on
2026-10-14; the documentation has moved under `elevenlabs.io/docs/eleven-agents/`):

| Topic | Finding |
|---|---|
| Browser SDK | `@elevenlabs/client` (npm; **1.28.0** on 2026-10-14): `Conversation.startSession({agentId \| signedUrl \| conversationToken, connectionType, clientTools, overrides, onConnect, onDisconnect, onMessage, onModeChange, onStatusChange, onError, …})`; voice sessions use **WebRTC** by default; methods `endSession`, `sendUserMessage`, `sendContextualUpdate`, `sendUserActivity` ("the agent will not attempt to speak for at least 2 seconds"), `setMicMuted`, `setVolume`, `getId`. No CDN build is documented; the package is an npm module. |
| Private-agent auth | `platform_settings.auth.enable_auth=true`; the browser then needs a server-minted credential: a **signed URL** (`GET /v1/convai/conversation/get-signed-url`, WebSocket, valid 15 min) or a **conversation token** (`GET /v1/convai/conversation/token?agent_id=…`, WebRTC, returns `token` + `conversation_id`; expiry undocumented). A hostname allowlist (≤ 10, exact match) is the alternative, not to be combined. |
| Agents API | `POST /v1/convai/agents/create`, `GET`/`PATCH`/`DELETE /v1/convai/agents/{id}`; `conversation_config.agent.prompt.{prompt, llm, temperature, tool_ids, custom_llm, reasoning_effort}`, `agent.{first_message, language}`, `tts.{voice_id, model_id}` (`model_id` default `eleven_v4_turbo`), `turn.{turn_timeout (default 7), turn_eagerness, turn_model (default turn_v3), soft_timeout_config}`, `conversation.{max_duration_seconds (default 600), client_events}`, `platform_settings.{auth, overrides.conversation_config_override}`. The `llm` enum has roughly 150 values and includes `custom-llm`. Inline `tools` are **deprecated in favour of `tool_ids`**. |
| Tools API | `POST /v1/convai/tools`, `PATCH`/`DELETE /v1/convai/tools/{id}`; a client tool is `{"type":"client", name, description, parameters (JSON schema), expects_response (default false), response_timeout_secs (default 20; 1–120 or -1), execution_mode (immediate \| post_tool_speech \| async), interruption_mode (allow \| disable_during_tool \| disable_during_tool_and_turn), pre_tool_speech (auto \| force \| off), tool_call_sound, tool_error_handling_mode}`. "When `expects_response` is `true`, the conversation blocks until the client responds." The semantics of `execution_mode: async` are not documented. |
| Client events | `user_transcript`, `agent_response`, `agent_response_correction` (the text actually spoken when the user interrupted), `client_tool_call`, `interruption`, `audio`, `ping`, `conversation_initiation_metadata`, among others. |
| Key scope | API keys are permission-scoped: provisioning and the token endpoint need the **Agents (`convai`) write** permission; the voice-message adapters need only text-to-speech / speech-to-text. A key without `convai_write` answers HTTP 401 `missing_permissions` (observed 2026-10-14). |
| Plan | Library voices via the API and the Agents platform require a **paid plan**; the free tier answers HTTP 402 `paid_plan_required` for a library voice (observed 2026-10-14). |
| EU residency | `api.eu.residency.elevenlabs.io`, Enterprise only, separate workspace and key (ADR-0136 §9); whether the Agents API is served on that host is **unverified**. |
| Price | The Agents "Speech Engine" is listed at $0.08 per minute on the public pricing page; LLM cost on top is not stated. To be verified on the real account before a tenant is onboarded. |

**Smoke and spike** (operator, 2026-10-14, real account, `@elevenlabs/client`
1.28.0, agent provisioned through the API with the two client tools):

- Voice messages: the built ElevenLabs adapter transcribed German correctly;
  `opus_48000_64` **arrives Ogg-containered** (`OggS`), so the adapter
  declares `audio/ogg` and Telegram needs no transcode for it. This closes
  the observation left open by ADR-0136 §9.
- With a blocking client tool whose handler slept 20 s: the agent spoke a
  bridging sentence before the call, stayed silent while waiting, relayed
  the result verbatim and **invented nothing** while under pressure. With
  the vendor's default 20 s tool timeout and a 20 s handler, the agent
  spoke a timeout sentence **and the late result still arrived and was
  spoken afterwards** — the timeout is a spoken apology, not a lost answer.
  A user who interrupted during the wait started a second tool call, and
  the late result of the first still landed and was spoken. The "result as
  a later user message" (`sendUserMessage`) variant of a non-blocking tool
  also produced a natural "here is what I found" turn; `sendContextualUpdate`
  alone was not tested. `sendUserActivity` held the agent. German speech
  quality of a library voice was judged very good by the owner.
  First agent words were measured at ~7.4 s after `startSession()` on a
  first start, a figure that includes the browser's microphone permission
  prompt and the WebRTC connect and is not a steady-state latency.

### Constraints

- English artefacts (ADR-0008). Accepted ADRs are immutable; this ADR amends
  by reference only.
- The turn lock stays process-global for this ADR; the call must not be
  blocked by #071.
- The vendor's agent talks to the *browser*; PortfoliFLOW's server never
  receives audio and is never called inbound by the vendor. Every piece of
  the conversation PortfoliFLOW keeps must therefore be relayed by the
  browser, through the user's authenticated session.
- Whatever reaches `prompts/` is subject to the prompt-composition pin:
  Shirley's own system prompt stays byte-identical.
- No persona name other than Shirley in rendered copy (UI-023).

## Decision

### 1. Two conversation layers, one brain of record

PortfoliFLOW gains a **voice call**: a real-time spoken conversation between
the user's browser and a vendor-hosted conversational agent that carries
Shirley's persona. The agent's language model (the *conversation layer*) is
the one chosen in the agent definition from the vendor's list; it holds the
Soul and a call addendum and **no program function**. It knows nothing about
the book. Every number, name, date, position, limit or chart comes from
**Shirley** — the ordinary `AIServiceCore` turn with its tools — reached
through exactly one client tool, `ask_shirley`. The conversation layer is
instructed to relay Shirley's answer and never to recompute, extend or guess
it.

A custom or self-hosted conversation model (the vendor's `custom-llm` option,
which needs an inbound OpenAI-compatible endpoint reachable from the vendor)
is **out of scope** for this ADR and is the named later stage that needs its
own inbound-authentication ADR (§12).

### 2. The call port — the seam a second vendor implements

The vendor-specific code sits behind two small interfaces, so the panel, the
`ask_shirley` job, the history logic and the Admin card are vendor-free from
day one:

- **Server side**, package `services/voice_call/`, a `CallProvider` Protocol
  with two operations: `ensure_agent(definition, agent_id | None) -> str`
  (create the vendor agent from the definition, or bring an existing one up
  to date, returning its id) and `session_credentials(agent_id) ->
  SessionCredentials` (a one-session credential for the browser:
  `token`, `conversation_id`, the vendor key `elevenlabs`). The v1
  implementation is `ElevenLabsCallProvider`, plain `httpx`, no vendor SDK,
  per-call `AsyncClient`, `xi-api-key` header, the `_status_detail` error
  pattern of the voice adapters (status plus ≤ 200 body characters, never
  the key). The package imports only the standard library and `httpx` and
  gets its own layering guard, so `services/voice/`'s formula and guard are
  untouched.
- **Browser side**, one adapter module per vendor
  (`web/static/js/call_elevenlabs.js`) behind the interface
  `{start(credentials, handlers), stop(), setMuted(bool)}` with handlers
  `onStatus(connecting | connected | disconnected)`,
  `onMode(listening | speaking)`, `onTranscript({role, text, final,
  corrected})`, `onToolCall(name, args) -> Promise<string>`, `onError(message)`.
  The panel module (`web/static/js/call_panel.js`) and everything above it
  know only this interface. The vendor SDK is **vendored** under
  `web/static/vendor/elevenlabs-client-<version>/` as one self-contained ES
  module (the version in the directory name, like every other vendored
  library); no CDN.

### 3. Delegation — the blocking client tool `ask_shirley`

The agent definition declares one client tool:

```
type: client · name: ask_shirley
description: Ask Shirley, who has the portfolio data. Use it for every
             question about numbers, names, dates, positions, limits or
             charts. Relay her answer; never answer such a question yourself.
parameters: { question: string (required) }
expects_response: true · response_timeout_secs: 90
pre_tool_speech: force · interruption_mode: allow · execution_mode: immediate
```

The pattern is **blocking with a forced bridging sentence**: the platform
makes the agent say one short sentence ("Moment, ich schaue nach.") *before*
the call, the conversation then waits, and the user may interrupt at any
time. The spike showed this to be natural enough, with no invented facts and
with late results still landing; the non-blocking alternative is recorded in
*Alternatives Considered*.

The browser's handler for `ask_shirley` is a PortfoliFLOW job:

1. `POST /chat/call/ask` with `{question, events}` — `events` are the final
   transcript lines the browser has not yet relayed (§5), so the history is
   ordered before the question is answered. The endpoint answers **202
   `{job_id}`** at once.
2. The job runs **one** `stream_response` turn on the session's history,
   with the same `ResolvedLLM`, tool context and briefed system prompt as a
   typed turn, under the lock, with the **tool allowlist** of §3.1, and
   consumes the events through the same recorder, chart-binding and trim
   helpers the SSE handler uses. The assistant message is appended to the
   history with `channel="call"`; the question is **not** appended — the
   spoken utterance that led to it is already there as the relayed
   transcript.
3. The job then produces the **speakable answer**: a second, short,
   non-streaming call on the same `ResolvedLLM` through the existing
   `run_synthesis` entry point with one forced tool `speak({text})` — prose
   only, no markdown, numbers rounded and written for the ear, at most ~60
   words, in the user's language. Deterministic post-processing cannot read
   figures aloud well; a model can. No new core method is added.
4. The browser polls `GET /chat/call/jobs/{job_id}` (every second) until
   `done`, `failed` or **75 s** have passed. On `done` it returns the
   speakable text to the agent and renders the **display payload** —
   the full prose and the turn's chart artefacts — into the chat history
   as the ordinary assistant bubble and chart figure. The job reports
   `waiting_for_turn` while it is parked behind the lock; after ~8 s of that
   the panel says so (§6).
5. On the 75 s timeout the browser returns a fixed fallback sentence ("Das
   dauert länger — ich schreibe die Antwort in den Chat.") and stops
   polling; the job **runs to completion on the server** and appends its
   answer to the history, so the sentence is literally true. The vendor's
   own 90 s timeout is never the first to fire. The two values are
   deliberately generous — a tool-calling turn with several reads can take
   half a minute, and a timeout that fires regularly would make the call
   feel broken; they are tuned on the implementation walk, with the
   PortfoliFLOW timeout always at least 15 s below the vendor's.
6. On **barge-in** the panel cancels: `POST /chat/call/jobs/{job_id}/cancel`
   closes the job's generator (the lock falls at its next `await`; a tool
   already running in a thread completes first) and discards its result —
   nothing reaches the history, mirroring ADR-0050's error-path rule. The
   handler returns an empty result so the agent does not speak a stale
   answer.

Job records live in `app.state.call_jobs`, keyed `(session_id, job_id)`,
bounded like the pending-turn store, single-worker like it.

#### 3.1 The tool allowlist — read-only enforced in code

`stream_response` gains a keyword `tool_names: frozenset[str] | None = None`.
When given, `_stream_response_locked` offers the model only those tool
definitions **and** `ToolRegistry.execute_tool` refuses any other name for
that turn (defence in depth: a model cannot call what it was not offered,
but the refusal is pinned regardless). The call job passes
`CALL_TOOL_ALLOWLIST`, a frozenset in `services/voice_call/`. Today it
equals the full set of sixteen registered tools, because every one is a
read; a test pins `CALL_TOOL_ALLOWLIST == set(registry.list_tools())` so the
first tool that writes — or any new tool — trips the test and forces a
decision. `web_research` stays in the set; the job timeout caps it.
Charts stay in the set; a chart is for the display payload. Read-only is
thus enforced by the allowlist and the existing fact that no tool writes,
not by the prompt alone; the Soul addendum (§9) adds the spoken pointer "I
can't do that here, please use the UI".

### 4. The call endpoints

All under `web/routes/chat.py`'s router (the call is chat furniture), all
with `require_session`, `require_role("owner", "member")` and `verify_csrf`
on writes, none reading the environment:

| Endpoint | Purpose |
|---|---|
| `POST /chat/call/session` | Resolve `voice_call` through the vault inside the tenant context; ensure the agent (§8); mint the session credential server-side; return `{vendor, token, conversation_id, max_duration_seconds, language}`. 503 with the misconfiguration message when the feature is on but incomplete; 404 when it is off. |
| `POST /chat/call/events` | Relay final transcript lines `[{seq, role, text, at}]`; appended to the history with `channel="call"` in `seq` order; duplicates by `seq` ignored. |
| `POST /chat/call/ask` | Start an `ask_shirley` job (§3); 202 `{job_id}`. |
| `GET /chat/call/jobs/{job_id}` | Job state: `waiting_for_turn`, `running`, `done` (+ speakable, prose, charts), `failed` (+ user-safe message), `cancelled`. |
| `POST /chat/call/jobs/{job_id}/cancel` | Cancel (§3, step 6). |
| `POST /chat/call/end` | Mark the call ended: appends the end line, triggers a history re-render. |

The dock context (`GET /chat/dock`) gains `voice_call_enabled`, resolved like
the voice flags: `voice_call.enabled` true **and** the provider key present
— three config reads, never a credential probe.

### 5. The call in the chat history

- `Message` gains an optional attribute `channel: str | None = None`
  (`"call"` for everything a call produced). `to_openai_messages` ignores
  it; the history partial renders a small "Call" chip on such bubbles.
- A call appends: one system-styled line "Call started HH:MM" when the
  session connects; every **final** user transcript as a `user` message;
  every **final** agent utterance as an `assistant` message — where the user
  interrupted, the vendor's corrected text (what was actually spoken) is
  used, not the planned one; every `ask_shirley` answer as an ordinary
  `assistant` message (prose and charts, §3 step 2); one line "Call ended
  HH:MM" at the end. Tentative transcripts are never stored. A pseudo-user
  message the browser would inject is never used (the blocking pattern
  needs none).
- The transcript appears in `#chat-history` **live**, appended by the panel
  as finals arrive, and is already on the server, so a reload rehydrates
  the same conversation.
- `ask_shirley` sees the same history as a typed question, including the
  call so far; a typed question after the call sees the call. The existing
  trim (20 messages / 24,000 characters) applies unchanged: a long call
  evicts older context first. This is accepted for v1 and recorded as a
  consequence.

### 6. The turn lock — posture, not fix

The process-global `_TURN_LOCK` is kept. A call's `ask_shirley` job can wait
behind another user's turn. This ADR (i) makes the wait **visible** — the
job reports `waiting_for_turn`, the panel says "Shirley is busy with another
request" after about 8 s, and the fixed bridging sentence has already
covered the first seconds; (ii) bounds it with the 75 s job timeout and the
spoken fallback of §3 step 5; (iii) names roadmap #071 as the successor that
removes the wait. The call strand is explicitly **not** blocked by #071.

### 7. The `voice_call` declaration — annex to ADR-0112 §3

```
provider="voice_call"
  api_key   secret  tenant   — the vendor key with the Agents write permission
  enabled   config  tenant   choices = {"true", "false"}   (a switch)
  agent_id  config  tenant   — written by PortfoliFLOW; leave empty to let it provision
  model     config  tenant   — the conversation-layer model id; empty = the definition's default
  voice     config  tenant   — vendor voice id; empty = voice_tts.voice if set
  language  config  tenant   — default "de"
  base_url  config  tenant   — default https://api.elevenlabs.io
managed_by_matrix=False · env_fallback=False · optional=True
```

- **No environment links**: no `_ENV_CREDENTIAL_FIELDS` or
  `_ENV_CONFIG_FIELDS` entry, exactly as `provider_channel` (ADR-0131). The
  absence of the link, not the flag, is what makes a deployment-wide switch
  impossible for a feature that ships audio to a vendor.
- **Own key**, not a reuse of `voice_tts.api_key`: the call needs a
  different permission scope (Agents write) than the speech adapters, keys
  rotate independently, and ADR-0112 §1 chains a credential as a unit per
  provider. The Admin hint says: "A key with the Agents write permission. It
  may be the same key as for text-to-speech if that key carries both
  permissions."
- **`model` is free text with a code default**, not a `choices` field: the
  vendor's model vocabulary has ~150 entries and changes monthly; a mirrored
  `choices` set would rot. The default lives in the agent definition; a
  wrong value fails loudly at provisioning with the vendor's message.
- **Availability** = `enabled` true and `api_key` present; completeness is
  checked at session start and fails with a message naming the card
  (`Admin → Providers & Credentials → Voice call`) and the missing field.
- **The Admin card** "Voice call" carries the description "A spoken
  conversation with Shirley in the browser — the on/off switch for this
  tenant. Audio and transcript go to the voice vendor; see the handbook.",
  the consumer-status pill "live — saves apply instantly", and a computed
  state line: *inactive — switch off* · *inactive — no key* · *active, agent
  not yet provisioned* · *active (agent …)*. The `voice` field's hint names
  the fallback to the text-to-speech voice.

### 8. Agent provisioning — definition in code, agent at the vendor

- `services/voice_call/agent_definition.py` holds the **definition**: the
  composed system prompt (§9), the `ask_shirley` tool (§3), `first_message`,
  `language`, `tts.voice_id`, `tts.model_id` (a conversational model),
  `turn` settings (`turn_timeout`, `turn_eagerness`, `turn_model` at the
  vendor defaults unless the walk says otherwise), `conversation.max_duration_seconds
  = 1200`, the client events the panel needs (`user_transcript`,
  `agent_response`, `agent_response_correction`, `client_tool_call`,
  `interruption`, `audio`, `ping`, `conversation_initiation_metadata`),
  `platform_settings.auth.enable_auth = true`, overrides **off**, and the
  agent `name` `PortfoliFLOW Shirley call · def <sha256[:8] of the definition>`.
- **Provisioning is lazy**, at `POST /chat/call/session`: with `agent_id`
  empty the provider creates the tool and the agent and PortfoliFLOW writes
  the id into the tenant's `voice_call.agent_id`; with an id present it
  fetches the agent, compares the hash in its name with the current
  definition's and PATCHes tool and agent when they differ. An in-process
  memo per tenant (`app.state`) remembers "verified for this hash" so the
  fetch happens once per process and definition. No second vault field, no
  hook in the Admin write path, no startup work.
- A changed definition therefore ships with a release and takes effect on
  the next call of each tenant. An operator who edits the agent in the
  vendor dashboard is overwritten on the next definition change; the
  handbook says so.
- Icons: `call` (Lucide `phone`), `call-end` (`phone-off`) and `mute`
  (`mic-off`) are vendored into `web/static/vendor/lucide/` and registered
  in `web/icons.py::ICONS`.

### 9. The Soul addendum — one new file, Shirley byte-identical

`prompts/Soul_Shirley_call.md` is composed *in code* onto the `## System
Prompt` section of `prompts/Soul_Shirley.md` for the vendor agent only.
Shirley's own composed prompt stays byte-identical (the existing hash pin);
a new test pins the composed call prompt by hash so an edit to either file
is a conscious change. The addendum states:

- identity — you *are* Shirley, on the phone, in a live spoken conversation;
- the **no-facts rule** — you know nothing about the book; every number,
  name, date, position, limit and chart comes from `ask_shirley`; relay her
  answer, never recompute, extend or guess; if she could not answer, say so;
- the bridging sentence — one short sentence before every `ask_shirley`;
- read-only — you cannot book, change or save anything; say "I can't do
  that here, please use the UI" and offer to look something up instead;
- language mirroring — answer in the user's language (German or English);
- brevity for the ear — short sentences, no lists, no markdown, numbers
  spoken naturally;
- the fallback — when `ask_shirley` reports it is taking longer, say the
  answer will appear in the chat and carry on.

### 10. Lifecycle, limits and failure modes

- **Start**: the dock's call button (composer row, beside the microphone,
  present only when `voice_call_enabled`) requests the session credential,
  then the adapter starts the vendor session; the panel shows *connecting*
  until the vendor reports connected, then *listening* / *speaking* after
  the agent's mode.
- **During**: mute toggles the microphone (`setMicMuted`); `ask_shirley`
  jobs show *working* with the busy variant of §6; results land in the
  history live.
- **End**: the end button, the vendor's `max_duration_seconds` (20 min),
  a tab close or a network loss all end the vendor session; the panel
  returns to idle, the end line is appended, the history re-renders. The
  transcript relayed so far is already in the history; the last utterance
  may be lost on an abrupt close, which is accepted.
- **Vendor unavailable** (credential refused, connect failure, SDK error):
  the panel shows the error state with a user-safe message; the chat is
  unaffected.
- **Nothing is persisted** beyond the chat history (which is in-memory per
  session, ADR-0050): no audio, no vendor conversation id, no job records
  beyond their lifetime. The vendor keeps its own conversation record under
  its terms; the handbook says so.

### 11. Compliance posture — what the card and the handbook say

The call sends the user's **audio and the transcript to the vendor**, where
the conversation model runs; PortfoliFLOW receives the transcript back
through the browser. The posture is stated in three places: the Admin card
description (§7), a line in the panel's connecting state ("Audio goes to the
voice vendor"), and the handbook section, which also names the paid-plan
requirement, the key permission, the EU-residency option with its Enterprise
restriction and the unverified status of the Agents API on that host, and
that the vendor retains conversation records under its own terms. The opt-in
is per tenant, by an owner, in the vault, nowhere else. This feature is what
fulfils the website's "phone" claim.

### 12. Conditions for a self-hosted conversation layer

A self-hosted or custom conversation model (the vendor's `custom-llm`, or a
local full-duplex stack behind the §2 port) is re-evaluated when all of the
following hold: German speech quality the owner judges acceptable; first
agent words within about one second; working barge-in; and a footprint that
runs on one GPU a tenant realistically has. That later stage needs an ADR of
its own for inbound authentication (a vendor calling a tenant's instance) or
for the local stack; the §2 port must admit it without a change to the panel,
the endpoints or the history logic.

## Rationale

- **One brain of record** keeps every fact on the path that is already
  tested, gated and briefed — tools, trust classes, case briefs, charts,
  history — and keeps the conversation layer honest by construction: it has
  no tool but `ask_shirley`, and the spike showed the model holds the
  no-facts rule under a 20 s silence.
- **Blocking with a forced bridging sentence** is the simplest pattern that
  is natural enough: the platform guarantees the sentence, the user can
  interrupt, and no pseudo-user message pollutes the transcript. The
  non-blocking pattern buys the agent's ability to chatter during the job at
  the price of a fake user turn and a second delivery path.
- **A client tool through the user's session** needs no inbound endpoint, no
  vendor-facing authentication, no new exposure of the instance; the browser
  already holds the session and the CSRF token. Audio never touches the
  server.
- **202 and polling** rather than a long-held request keeps the job alive
  across the browser's timeout, lets the panel show the lock wait, and makes
  "I'll put it in the chat" true.
- **Own declaration, own key, no env link**: the ADR-0131 precedent exists
  precisely for a feature that must never be switched on for every tenant by
  a deployment variable; the key scope difference settles the key question
  on its own.
- **Lazy provisioning with the hash in the agent name** keeps the vault
  schema at one PortfoliFLOW-written field, needs no Admin write-path hook,
  and still guarantees the agent at the vendor matches the definition in the
  repository.
- **Vendoring the SDK** follows the project's vendor posture and spares the
  operator a CSP origin for a CDN; the WebRTC and API origins remain the
  operator note they must be.
- **The allowlist test as tripwire** turns "read-only" from a prompt
  promise into a failing test the day a write tool appears.

## Alternatives Considered

- **Non-blocking delegation** (`expects_response: false`; the browser runs
  the job and delivers the result as a later user message or contextual
  update). Rejected for v1: the spike showed the blocking pattern natural
  enough; the non-blocking one injects a user message the user never said,
  which the history logic would have to filter, and it doubles the delivery
  paths. Kept as the fallback should tuning show the silence grates.
- **A webhook (server) tool instead of a client tool.** Rejected: it needs
  an inbound endpoint on every instance reachable from the vendor, a new
  authentication scheme and a public exposure; the owner's decision is the
  client tool.
- **The vendor's `custom-llm`** (Shirley herself as the conversation
  model). Rejected for v1: inbound, OpenAI-wire, streaming, and it would make
  the latency of a tool-calling turn the latency of every spoken reply.
  Named as the later stage of §12.
- **Full-duplex voice-to-voice models directly from PortfoliFLOW** (OpenAI
  Realtime, Gemini Live), as ADR-0076 considered. Rejected: the same
  "replaces the brain" problem, and no persona-voice continuity with the
  ElevenLabs voice-message path; the §2 port admits such a vendor later.
- **Loading the SDK from a CDN.** Rejected: against the vendor posture, adds
  an origin to every CSP, and makes the SDK version a moving target.
- **Managing the agent in the vendor dashboard** only. Rejected by the
  owner's decision: the definition lives in the repository; a hand-edited
  agent would drift from the Soul and the tool contract.
- **Reusing `voice_tts.api_key`** for the call. Rejected: different
  permission scope, independent rotation, ADR-0112 §1's unit chaining.
- **A `choices` field for the model.** Rejected: a ~150-entry vocabulary
  that changes monthly cannot be mirrored and pinned the way the
  three-entry voice vocabulary is.
- **Fixing the turn lock now.** Rejected: that is roadmap #071 with its own
  ADR; this strand makes the wait visible and bounded and must not be
  blocked by it.
- **Storing the definition hash in a second vault field.** Rejected in favour
  of the agent name: one PortfoliFLOW-written field is enough.
- **Do nothing** (voice messages only). Rejected: the website promises the
  phone mode and the owner has decided to build it.

## Non-goals

- Fixing the process-global turn lock (roadmap #071).
- A custom or self-hosted conversation model; a local call adapter (§12).
- Telegram calls — impossible for a bot (ADR-0076) and excluded by the owner.
- Writing actions by voice; a spoken confirmation design.
- A second call vendor (OpenAI Realtime, Gemini Live, Deepgram) — the port
  admits it, nothing is built.
- Restyling the dock or the Assistants area (UX strand).
- Streaming or sentence-chunked TTS for voice *messages* (ADR-0076 follow-up,
  still open).
- Persisting calls beyond the in-memory chat history; recording audio.

## Consequences

### Positive

- The website's phone mode exists: a fluid German conversation with
  Shirley, interruption included, with every fact from the tested tool path.
- Per-tenant opt-in with no deployment-wide switch, own key, no env link —
  the ADR-0131 shape.
- Vendor code sits behind a two-method server port and a five-method
  browser adapter; the panel, the job and the history are vendor-free.
- Read-only is a failing test, not a prompt promise.
- The call transcript is part of the chat; typed and spoken turns share one
  history and one brief.
- ADR-0136 §9's open observation is closed on the way: Opus arrives
  Ogg-containered.

### Negative

- Audio and transcript leave the tenant for the vendor; EU residency is
  Enterprise-only and unverified for the Agents API. Privacy-focused tenants
  forgo the call (owner's accepted trade-off).
- A paid vendor plan and a permission-scoped key are prerequisites; the
  handbook must say so.
- A long call evicts older chat context under the unchanged trim.
- An `ask_shirley` job behind another user's turn waits; the wait is
  visible and bounded, not removed (#071).
- The SDK bundle (WebRTC stack included) is a sizeable vendored file to keep
  current.
- Two LLM calls per question (the tool turn and the speakable rewrite): a
  few seconds and a small cost per spoken answer.
- Steady-state first-words latency is not yet measured; the connecting
  state covers it, tuning days may adjust `turn` settings.

### Neutral / Follow-ups

- Roadmap #071 — per-user / per-tenant turn lock — removes the busy wait.
- The §12 conditions for a self-hosted conversation layer; the inbound-auth
  ADR for `custom-llm`.
- Verify the Agents API on the EU-residency host before an EU-residency
  tenant is onboarded; verify the per-minute and LLM cost on the real
  account.
- Whether `execution_mode: async` offers a native non-blocking pattern worth
  adopting (undocumented today).
- Pin the vendored SDK version in the handbook and note the upgrade step.

## Implementation Notes

- **New** `services/voice_call/`: `__init__.py`, `port.py` (`CallProvider`,
  `SessionCredentials`, `AgentDefinition`), `agent_definition.py` (the
  definition, the prompt composition, the hash), `elevenlabs_provider.py`
  (tools and agents API, token), `allowlist.py` (`CALL_TOOL_ALLOWLIST`),
  `speakable.py` (the forced `speak` tool for `run_synthesis`),
  `jobs.py` (job records and runner), `errors.py`.
- `services/ai_service_core.py`: `tool_names=` keyword on `stream_response`
  / `_stream_response_locked`; `services/tool_registry.py`: per-turn
  allowlist honoured in `execute_tool`.
- `services/ai_models.py`: `Message.channel`.
- `services/credential_vault/taxonomy.py`: the `voice_call` declaration;
  `services/investments/credential_resolver.py`: **no** entries.
- `web/routes/chat.py`: the six endpoints of §4, `voice_call_enabled` in the
  dock context, the per-session call resolution twin of `_resolve_stt`.
- `web/templates/_partials/shirley_dock.html` (call button),
  new `_partials/call_panel.html`, `_partials/chat_history.html` (chip,
  start/end lines), `web/static/js/call_panel.js`,
  `web/static/js/call_elevenlabs.js`, `web/static/css/` (panel), vendored
  `web/static/vendor/elevenlabs-client-<version>/`, three Lucide icons,
  `web/icons.py`.
- `web/routes/provider_credentials.py` and the card partial: labels,
  description, hints, consumer status, the "Voice call" state line.
- `prompts/Soul_Shirley_call.md`.
- `docs/operator-handbook.md` §9, `CHANGELOG.md`, `docs/roadmap.md` (#077),
  `docs/adr/README.md`.
- **Tests**: `tests/services/voice_call/` (port contract, definition hash,
  provider against `pytest-httpx`, allowlist tripwire, speakable, jobs,
  layering guard), `tests/assistants/test_prompt_composition.py` (call
  prompt hash; Shirley unchanged), `tests/assistants/test_tool_registry.py`
  (allowlist refusal), `tests/web/test_chat_call.py` (endpoints, 202/poll/
  cancel/timeout, events ordering, history channel, dock flag),
  `tests/services/credential_vault/test_taxonomy.py` (declaration; no env
  link), `tests/web/test_provider_credentials.py` (card), `tests/web/
  test_icons.py`. Guards that must stay green: `test_web_no_env_reads.py`,
  `test_ai_service_core_qt_free.py`, `test_voice_layering.py`, TX-01…TX-08,
  the chat route suites, UI-023.
- **Operator notes**: a paid vendor plan; a key with the Agents write
  permission; CSP origins if the proxy sets one — `api.elevenlabs.io`
  (REST and WebSocket) and the vendor's WebRTC endpoints — and the
  microphone's secure-context requirement already noted for voice messages.

## Compliance & Audit Relevance

- **ISO 25010:** Security (no inbound endpoint; the vendor is reached only
  through the user's authenticated session; keys per tenant, resolved per
  session, masked; read-only pinned by test), Reliability (every failure
  surfaces in the panel or the card; no silent fallback; bounded job),
  Portability (two-method server port, five-method browser adapter,
  vendored SDK), Maintainability (definition in code, hash-verified at the
  vendor).
- **Regulatory references:** audio and transcripts of portfolio
  conversations leave the tenant for a third-country vendor unless the EU
  host is used; this is the tenant's data-residency decision under its
  DORA/MaRisk framing, surfaced on the card and in the handbook, and taken
  per tenant by an owner. PortfoliFLOW stores no audio; the transcript lives
  in the in-memory chat history only.
- **Audit evidence:** the `voice_call` declaration and its taxonomy tests
  (no env link); the allowlist tripwire test; the prompt-composition hash
  tests; the provider tests at the transport; the job tests (timeout,
  cancel, history append); the card copy; the handbook section; this ADR.

## References

- Related ADRs: ADR-0022 (tool trust classes), ADR-0031 (turn lock),
  ADR-0037 (vendor posture), ADR-0038 (Qt-free core), ADR-0050 (chat
  history), ADR-0076 (voice I/O; "voice-native Shirley" track), ADR-0112
  (scoped settings; §3 amended), ADR-0114 (chart artefacts), ADR-0118 and
  ADR-0136 (voice taxonomy and providers), ADR-0131 (config-only, no-env-link
  precedent), ADR-0133 (prompt composition rules).
- ElevenLabs — JavaScript SDK; Agent authentication; Tools API (create);
  Tool interruptions; Tool call sounds; Conversation flow; Agents API
  (create); Get conversation token; Changelog 2026-04-27 — all read
  2026-10-14.
- Operator smoke and spike records of 2026-10-14 (closing report of the
  concept chat).

---

## Revision History

| Date       | Author                     | Change                          |
|------------|----------------------------|---------------------------------|
| 2026-10-14 | PortfoliFLOW project owner | Initial draft, status Proposed. |
| 2026-10-14 | PortfoliFLOW project owner | Status Accepted.                |
| 2026-10-19 | PortfoliFLOW project owner | §4/§7 as built: "key present" is read as the card's completeness state, not probed per render; the call button gates on `voice_call.enabled` alone, as ADR-0118 §5, and a missing key is judged at the first start (`/chat/call/session` 503 naming the card). Owner decision 2026-10-14. |
| 2026-10-19 | PortfoliFLOW project owner | Implementation Notes as built: the job runner lives in `web/routes/chat.py`; `services/voice_call/jobs.py` holds the job records, the state machine and the bounded store, as §2's layering rule requires. |
| 2026-10-19 | PortfoliFLOW project owner | §5 as built: the "Call started" / "Call ended" lines are history furniture in a per-session sidecar beside the history (ADR-0114 pattern), anchored to the last message and evicted with its turn group — not `Message`s; `to_openai_messages` is untouched and no model ever sees them. |
| 2026-10-19 | PortfoliFLOW project owner | §4 as built: the session response adds `call_id`, carried by the events, ask and end requests; the relay's `seq` cursor is kept per `(session, call_id)`. |
| 2026-10-19 | PortfoliFLOW project owner | §3 step 2 as built: the model-bound conversation is the session's history plus the agent's question as a transient user message; neither the question nor that message is stored. |
| 2026-10-19 | PortfoliFLOW project owner | §3 step 5 as built: "stops polling" is read as "stops waiting" — at 75 s the agent gets the fallback sentence at once, and a slow display poll (3 s, at most ten minutes) renders the late answer when it lands. |
| 2026-10-19 | PortfoliFLOW project owner | §2 as built: the browser adapter takes its handlers at the factory (`window.pfCallAdapters[vendor](handlers)` → `{start, stop, setMuted, inputVolume}`); corrections and interruptions are their own callbacks (`onCorrection`, `onInterruption`), and `inputVolume()` feeds the level meter. Every SDK callback is guarded; a disconnect before `connected` is reported as a start failure with the SDK's message. |
| 2026-10-19 | PortfoliFLOW project owner | §3/§9 as built: the `ask_shirley` description names news and press coverage; the addendum carries two rules beyond §9's list (news relayed with Shirley's source and caution; no uploads or long texts on a call, the chat shows them); the call prompt closes with a generated capability block "What Shirley can look up" in plain language and without tool names, so the agent delegates press questions too (walk finding 2026-10-16). Shirley's own prompt stays byte-identical. |
| 2026-10-19 | PortfoliFLOW project owner | §7 as built: `voice_tts.voice` stands in for an empty call voice only when `voice_tts.provider` is `elevenlabs`; a voice of another provider is not a call voice, and the start fails naming the card. A library voice with live moderation cannot be used for agents (vendor HTTP 400 at agent creation); the Admin hint says so. |
| 2026-10-20 | PortfoliFLOW project owner | §8 as built: before an agent is created, the account's agents are searched by the definition name and an agent carrying the current hash is adopted; first starts of one tenant are serialised by an in-process lock; a failed token mint drops the tenant's memo entry. Owner-accepted hygiene. |
| 2026-10-24 | PortfoliFLOW project owner | §2/§4 as built: the port carries one read beside its two writes, `catalog()` — the vendor's conversation models and the account's voices — for the Admin card's Load models / Load voices buttons; the fields stay free text, a click is a fetch, the card still probes nothing per render. Owner request 2026-10-24. |
