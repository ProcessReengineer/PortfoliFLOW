# ADR-0075: Multimodal Image Input for Shirley — Vision on the Web and Telegram Surfaces

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** PortfoliFLOW project owner
- **Tags:** ai-service, shirley, web-migration, telegram, multimodal, demo

---

## Context

Shirley's canonical identity (`docs/Soul_Shirley.md`) already promises a
capability she cannot currently exercise:

> Accept and process uploaded files (Excel, PDF, images) as context for
> analysis.

The intended use cases are demo-defining: an operator photographs a term
sheet of a new opportunity and asks how well it fits the portfolio; or
photographs a fund fact sheet and asks Shirley to read the figures *against
everything she already knows about that fund and the portfolio* — better or
worse than expected, and what it means for the book. The strategic value is
not OCR; it is **vision plus Shirley's portfolio tools in a single streamed
turn**.

Reading the code confirms the engine for this is already present and tested,
but the entry channels are not wired:

1. **The data model already supports images.** `services/ai_models.py`
   defines `Attachment(filename, mime_type, data: bytes)` and
   `Message.attachments`. `Conversation.to_openai_messages()` already
   serialises `image/*` attachments on `USER` messages into the OpenAI/
   OpenRouter vision shape:

   ```python
   {"type": "image_url",
    "image_url": {"url": "data:<mime>;base64,<b64>"}}
   ```

   This is not incidental: it is pinned by
   `tests/assistants/test_ai_service.py::test_attachment_becomes_multimodal_content`.

2. **The core carries the image through the tool loop.**
   `AIServiceCore._stream_response_locked` builds
   `messages = conversation.to_openai_messages()` **once** and replays the
   same list across every tool iteration. A vision block therefore coexists
   with `READ_INTERNAL` tool calls (`get_portfolio_overview`,
   `get_investment_data`, `get_saa_configuration`, …) within one turn — the
   exact shape the demo scenario needs. Claude models on OpenRouter accept
   vision and tools in the same request.

3. **The configured models are vision-capable.** Both surfaces run
   `anthropic/claude-*` via OpenRouter (`shirley_model`); Claude processes
   `image_url` blocks natively. `config/scraper_model_capabilities.json`
   already encodes the `anthropic/claude-*` family as multimodal (for PDFs).

The three gaps are at the edges, not the core:

- **Web.** `web/routes/chat.py::post_message` has the signature
  `message: str = Form(...)` and constructs `Message(role=USER, content=text)`
  with no attachments. There is no multipart path. The composer
  (`_partials/shirley_section.html`) and `static/js/chat.js` have no
  attachment control.
- **Telegram.** `bot/telegram_bot.py::_handle_text_message` reads
  `incoming_text = message.text or ""` and, for any non-text message,
  executes `if not incoming_text: return` — photos are silently dropped.
  (The bot already *sends* images out: chart artefacts via
  `BufferedInputFile`. Only the inbound direction is missing.)

Two secondary concerns are not blockers but must be handled deliberately:

- **History token bloat.** If an image stays in the persisted conversation,
  `to_openai_messages()` replays the base64 payload on every subsequent
  turn — there is no analytic benefit once Shirley's textual reading is in
  history, only token cost.
- **Silent vision drop.** If a non-vision model were ever configured,
  OpenRouter may strip a malformed/unsupported content part rather than
  rejecting the request — the exact failure shape `services/scraper/
  capabilities.py` was built to prevent. The chat path has no equivalent
  guard.

This decision touches no security boundary (no new tenant seam, no new
trust class) and does not modify the `_TURN_LOCK` concurrency control.

## Decision

PortfoliFLOW enables **raster-image (photo) input to Shirley on both the web
chat surface and the Telegram bot** by populating `Message.attachments` with
`image/*` `Attachment`s and relying on the existing, tested
`Conversation.to_openai_messages()` vision serialisation. The Qt-free core
(`services/ai_service_core.py`), the tool-execution loop, and `_TURN_LOCK`
are **not modified**.

### Scope

- **In scope:** JPEG, PNG, WebP, and (non-animated) GIF raster images, sent
  with optional accompanying text. One or more images per turn.
- **Out of scope (separate item):** PDFs and other documents sent *to
  Shirley*. Those require the scraper's `openrouter_file` content-block shape
  (`{"type": "file", "file": {...}}`), not `image_url`, because
  `to_openai_messages()` renders non-image attachments only as text
  placeholders. The Report Scraper already owns that path for its one-shot,
  tool-free extraction flow; routing Shirley's streamed, tool-enabled turns
  through it is explicitly rejected below.

### Web surface

`POST /chat/messages` accepts optional `UploadFile`s alongside the text
field (multipart form). The route validates each file's MIME type and size,
builds `Attachment`s, and attaches them to the **same** user `Message` it
already appends to the session's in-memory conversation before stashing the
turn. Because `GET /chat/stream/<turn_id>` reads that same conversation
object (the user message is appended in the POST, per ADR-0050), **no
separate byte stash is required** — the images are already present when the
SSE stream opens.

The composer gains a file input (`accept="image/*"`, `multiple`,
`capture="environment"` so mobile clients open the camera directly); the
`required` constraint on the textarea is removed so an image-only message is
valid (a default instruction is supplied server-side when text is empty).
The HTMX form is marked `hx-encoding="multipart/form-data"`. The
`turn_started.html` fragment renders inline thumbnails in the user bubble.

### Telegram surface

The single message handler is extended to accept `message.photo` (largest
`PhotoSize`) and image-typed `message.document`. The bot downloads the file
bytes via the aiogram v3 download API, uses `message.caption` as the text
content (falling back to a default instruction when absent), builds an
`Attachment`, and attaches it to the turn's user `Message`. A message that
is neither text nor a supported image is still silently ignored.

### Single-turn vision contract (history hygiene)

A turn's images live only for the turn in which they are sent. After a
successful turn, the persisted user message keeps its **text only**; its
attachment bytes are dropped:

- Web: the SSE handler clears `attachments` on the turn's user message after
  appending the recorded assistant/tool messages, before trimming. (The trim
  math in `_trim_history` already counts only `m.content`, so it is
  unaffected either way.)
- Telegram: the bot already persists text-only `Message`s into
  `_chat_histories`; the inbound `Attachment` is never written there.

This bounds token growth, keeps the multi-turn replay in
`to_openai_messages()` cheap, and matches the existing rehydration contract
(ADR-0050): rehydrated history is text-only on both surfaces, so a reloaded
conversation shows Shirley's reading of the image, not the image.

### Vision-capability gate

A small, data-driven helper `services/vision_capabilities.py::supports_vision(model_id)`
matches the active model against an fnmatch allowlist in
`config/vision_capable_models.json` (same mechanism and "no silent fallback"
discipline as `services/scraper/capabilities.py`). When an image is attached
but the active model is not vision-capable, each surface surfaces a clear,
human-readable message **instead of** letting OpenRouter silently drop the
image block. Text-only turns never consult the gate.

### Size guard

A shared `MAX_IMAGE_BYTES` ceiling (default 8 MB per image, before the ~33 %
base64 inflation) is enforced on both surfaces; oversize images are rejected
with a clear message. Server-side downscaling is **deliberately not**
introduced (it would add a Pillow dependency the project does not currently
carry); it is recorded as an optional future enhancement.

## Rationale

- **Minimal blast radius.** The high-risk core — the streaming loop, the
  process-global `_TURN_LOCK`, the `ToolRegistry` brackets — is untouched.
  The change is three edges (web route, composer/JS, Telegram handler) plus
  two small, well-isolated helpers (vision gate, byte-strip). The single
  most load-bearing fact is that `to_openai_messages()` already produces the
  correct vision shape and is already tested.
- **Right format for the payload.** Photos are images: `image_url` is
  correct. `openrouter_file` is the document path and belongs to the
  scraper's one-shot flow.
- **Vision *and* tools in one turn** is the demo value, and it only exists on
  the `stream_response` path — never on the scraper's tool-free one-shot
  path.
- **Single-turn vision** is the cheapest contract that preserves both
  correctness (valid OpenAI replay) and the existing text-only rehydration
  behaviour.

## Alternatives Considered

1. **Route images through the scraper's `build_extraction_messages` /
   `openrouter_file` path.** Rejected. That path is synchronous, tool-free,
   non-streaming, and returns a structured extraction — it cannot call
   Shirley's portfolio tools and cannot stream. The entire strategic value
   (read the image *and* reason over the live portfolio via tools) would be
   lost.
2. **Persist images in conversation history.** Rejected. Replays the base64
   payload every turn for no analytic gain after the first turn; unbounded
   token cost; no benefit given the text-only rehydration contract.
3. **A separate "vision" endpoint/surface distinct from chat.** Rejected.
   Fractures the single-conversation UX, duplicates the tool-execution
   context plumbing (ADR-0047/0063), and contradicts the Soul prompt's
   single-assistant framing.
4. **Persist image bytes to Postgres / disk for full rehydration.**
   Deferred. Not demo-critical; rehydrated history is text-only today
   anyway (ADR-0050). Revisit only if image recall across reloads becomes a
   real requirement.
5. **Server-side downscaling on ingest (Pillow).** Deferred. Adds a
   dependency for a marginal payload benefit; the size guard plus
   client-side camera capture covers the demo need.

## Consequences

### Positive

- A high-impact demo capability — photograph a term sheet or fact sheet and
  get portfolio-contextual analysis — with a tiny core footprint.
- Reuses a path that is already implemented and unit-tested; lowest-risk way
  to ship vision.
- The vision gate hardens the chat path against the silent-drop failure mode
  the scraper already guards against.

### Negative

- Vision turns carry larger request payloads and cost more tokens than
  text-only turns.
- Reloading a conversation will not redisplay the image (text-only
  rehydration, consistent with ADR-0050); this is a known, accepted limit.
- HEIC images straight from some iPhone configurations are not in the
  supported set; the client/Telegram typically transcodes camera captures to
  JPEG, but a HEIC document upload is rejected with a clear message rather
  than silently mishandled.

### Neutral / Follow-ups

- PDF (and other document) input *to Shirley* via the `openrouter_file` path
  remains a separate, smaller follow-up item.
- Multi-image turns are supported by the model; the UI may cap the count per
  turn for usability. Not gated by this ADR.
- Optional Pillow-based downscaling and Postgres-backed image recall are
  recorded as future enhancements.
- The `_TURN_LOCK` process-global and the `resolve_tenant_id()` single-tenant
  seam are unchanged and out of scope here.

## Implementation Notes

- Affected modules / files:
  - `services/ai_models.py` — **no behavioural change**; the existing
    `image/*` serialisation is the contract this ADR relies on. (An optional
    convenience helper to clear attachment bytes may be added, or the routes
    may clear `Message.attachments` directly.)
  - `services/vision_capabilities.py` (new) — `supports_vision(model_id)`
    over an fnmatch allowlist; `MAX_IMAGE_BYTES`; `ALLOWED_IMAGE_MIME_TYPES`.
    Mirrors `services/scraper/capabilities.py` ("no silent fallback").
  - `config/vision_capable_models.json` (new) — fnmatch patterns for
    vision-capable model families.
  - `web/routes/chat.py` — `post_message` accepts `images: list[UploadFile]`;
    validates MIME + size + vision gate; builds `Attachment`s onto the user
    `Message`; passes thumbnail data URIs to `turn_started.html`. The SSE
    handler clears the turn's user-message `attachments` after
    `stream_finished`.
  - `web/templates/_partials/shirley_section.html` — file input in the
    composer; `hx-encoding="multipart/form-data"`; `required` dropped from the
    textarea.
  - `web/templates/partials/turn_started.html` — inline image thumbnails in
    the user bubble.
  - `web/static/js/chat.js` — minimal: clear the file input on form reset;
    no change to the EventSource flow.
  - `web/static/css/components/chat.css` — thumbnail styling.
  - `bot/telegram_bot.py` — extend the handler for `message.photo` /
    image `message.document`; download bytes; caption→content; size + vision
    gate; build `Attachment` on the turn's user message; persist text-only.
- Related tests (to add):
  - `tests/services/test_vision_capabilities.py` — allowlist match / no-match,
    size constant, MIME set.
  - `tests/web/test_chat_image_upload.py` — multipart accepted; `Attachment`
    built; bytes stripped after a finished turn; oversize rejected;
    non-vision model rejected; image-only message (no text) accepted with the
    default instruction.
  - `tests/bot/test_telegram_image_input.py` — photo and image-document build
    an `Attachment`; caption used as content; oversize and non-vision
    rejected; history persisted text-only; unauthorised sender still dropped.
  - `tests/assistants/test_ai_service.py` — existing
    `test_attachment_becomes_multimodal_content` continues to guard the
    serialisation contract (no change required).
- Layering: `services/vision_capabilities.py` imports only the stdlib and
  reads its JSON config; it must not import from `web/` or `bot/` and must
  not import PyQt6 (ADR-0038). The web route and the bot import *from* it.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Functional Suitability (a
  promised capability is delivered), Reliability (the vision gate prevents a
  silent-drop failure mode; the single-turn contract keeps replay valid),
  Maintainability (one shared, data-driven capability helper rather than two
  hard-coded surface checks).
- **Regulatory references:** None directly. No tenant-isolation change (the
  existing per-turn tool-execution context and RLS posture of ADR-0035/0047/
  0063 are unchanged). Image bytes are processed in memory for the duration
  of one turn and not persisted, which keeps the data-handling surface for
  potentially sensitive material (term sheets) minimal and auditable.
- **Audit evidence:** `services/vision_capabilities.py` (the single,
  data-driven vision gate); the attachment-strip step in
  `web/routes/chat.py::chat_stream`; the test suite listed above; this ADR.

## References

- Related ADRs: ADR-0038 (Qt-free core — the serialisation this ADR relies on
  lives in the model layer the core consumes), ADR-0047 (tool-execution
  context — the channel that lets the same vision turn also run portfolio
  tools), ADR-0048 (Shirley chart artefacts — the outbound image path the bot
  already uses), ADR-0050 (in-memory multi-turn history — the text-only
  rehydration contract this ADR preserves), ADR-0051 (Shirley embedded in
  Assistants), ADR-0027 (Report Scraper — owns the `openrouter_file` document
  path that this ADR deliberately does not reuse), ADR-0022 (tool trust
  classes — unchanged; no new tool is registered).
- Code referenced: `services/ai_models.py::Conversation.to_openai_messages`,
  `services/ai_service_core.py::_stream_response_locked`,
  `web/routes/chat.py`, `bot/telegram_bot.py`,
  `services/scraper/capabilities.py`, `docs/Soul_Shirley.md`.

---

## Revision History

| Date       | Author                     | Change                          |
|------------|----------------------------|---------------------------------|
| 2026-06-04 | PortfoliFLOW project owner | Initial draft, status Proposed. |
| 2026-06-04 | PortfoliFLOW project owner | Implemented across the web and Telegram surfaces (vision gate, multipart image upload + Attachment serialisation, Telegram photo/image-document input); single-turn, in-memory, non-persisted image contract. Status Accepted. |
