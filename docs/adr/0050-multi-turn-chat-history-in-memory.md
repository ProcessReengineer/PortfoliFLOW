# ADR-0050: Multi-Turn Chat History — In-Memory, Per-Session, Bounded

- **Status:** Accepted
- **Date:** 2026-05-15
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, integration, ui

---

## Context

The Phase-6 web chat surface was single-turn by construction. Each
user message arrived at `POST /chat/messages`, was stashed in the
short-lived `pending_turns` store, and was driven through
`AIServiceCore.stream_response` as a freshly built `Conversation`
containing exactly one `user` message. The SSE handler returned the
result and dropped everything.

Smoke testing across Prompts 6 / 6b confirmed that the tools work
mechanically — `get_investment_data → render_chart` produces the
expected Plotly figure, the Postgres-native investment tools resolve
correctly under the tool-execution context (ADR-0047), and the
two-axis chart pipeline (ADR-0048) is wire-correct. What did *not*
work was follow-up phrasing. "Then chart Investment A" and "now the
cashflows" arrived as cold-start questions with no anchor; Shirley
had to ask which investment was meant even though the user had
named it in the previous turn.

Multi-turn history is the prerequisite for the Assistants-area
embedding (Prompt 8) and for any usable demo session. It is also
the architectural step before the multi-thread management work
(named conversations, switching between threads, search, export),
which depends on a persistent store and is deliberately deferred
here.

The earlier infrastructure for cross-turn coordination is already
in this shape: `pending_turns` is per-session, in-memory, bounded
LRU, attached to `app.state`; `_TURN_LOCK` (ADR-0031) is process-
global. The history store follows the same precedent so the
single-worker boundary is consistent across every cross-turn seam.

## Decision

Per-session in-memory chat history, bounded (20 messages /
24 000 characters, role-safe FIFO), held on
`app.state.chat_histories`, lifecycled by login / logout / explicit
"new chat" / page reload.

The store keys on the authenticated session UUID and holds at most
100 sessions (bounded LRU at the session level). Each history is
a `services.ai_models.Conversation` carrying the full OpenAI message
vocabulary (`user` / `assistant` / `assistant`-with-`tool_calls` /
`tool`) so tool-call traces from earlier turns inform later ones.

The flow is:

- `POST /chat/messages` appends the user line to the session's
  history *before* returning the turn-started fragment, so the SSE
  handler sees the populated history when the stream opens.
- `GET /chat/stream/<turn_id>` drives the turn against the session's
  full history, reconstructs the assistant + tool messages emitted
  during the turn via a `_TurnRecorder` helper, and appends them
  on `stream_finished`. On `error` or client disconnect the partial
  assistant content is dropped — only the user message persists, so
  a retry re-asks the same question.
- `POST /chat/new` clears the session's history and returns the
  empty-state fragment.
- `GET /chat/history` returns the session's history rendered as
  `chat-message` partials; `chat.html` triggers it on load so a
  tab reload restores the conversation.
- Logout drops the session's history server-side.

Trim policy operates in turn-groups (one `user` plus its following
`assistant`/`tool` messages, up to the next `user`). Dropping a
whole group preserves the OpenAI invariant that every `tool` message
is preceded by a matching `assistant`-with-`tool_calls` entry.
Naïve single-message FIFO eviction would orphan `tool` messages and
400 the next API call.

Tool-data handles are *not* persisted across turns. The per-turn
data cache in `services/tools/_tool_context.py` continues to clear
in the SSE handler's `finally` block. The orchestration context
(`docs/Shirley_ToolOrchestration_Context.md`) instructs Shirley to
re-fetch from `get_investment_data` rather than reuse a stale
handle.

## Rationale

- **Simplest design that produces real multi-turn behaviour.**
  No schema changes, no migration, no new repository, no new
  services. The whole feature is a per-session `Conversation`,
  a trim function, two new endpoints, and a small SSE-side
  recorder.
- **Same operational seam as the existing cross-turn state.**
  `pending_turns` and `_TURN_LOCK` already establish that the
  single-worker phase tolerates process-local cross-turn state;
  the history follows the precedent and inherits the same
  migration triggers without inventing new architectural ground.
- **Server restart is operationally rare; logout is the user-
  facing reset.** The user expects to lose history on logout
  (and gets that). A server restart is uncommon enough — and
  produces a benign empty-state on reload — to not justify
  Postgres persistence at this phase.
- **Role-safety eviction is non-negotiable.** OpenAI's API
  rejects orphan `tool` messages; any FIFO that drops one
  message at a time risks producing a 400. Turn-group eviction
  is the smallest correct rule.

## Alternatives Considered

- **Postgres persistence now** (`chat_threads` + `chat_messages`
  tables, a repository, an async cleanup job). Rejected for this
  strand because multi-thread management is the actual use case
  that justifies the schema, and adding the table without the
  threads UI would be design surface without payoff. Migration
  trigger 2 below makes the persistence work explicit when it is
  needed.
- **Per-tab isolation** (e.g. issue a tab id and key on
  `(session, tab)`). Rejected because the existing `_TURN_LOCK`
  already serialises concurrent turns from the same session and
  two tabs reading the same history is the expected behaviour for
  most users.
- **Keep chart artefacts in the history.** Rejected because the
  streaming core strips the artefact envelope before the LLM-bound
  `tool_result` content is appended (ADR-0048). Reintroducing
  artefacts would require either a parallel store or
  re-architecting the recorder — both belong in the
  artefact-rehydration strand alongside Prompt 8.
- **Resume in-flight turns on reconnect.** Rejected. The user
  message remains in the history; the assistant side is dropped on
  disconnect. The simpler contract is "type again to retry".

## Consequences

### Positive

- Follow-up phrasing ("now the cashflows", "do the same for the
  next one") resolves naturally from prior turns.
- Page reload restores the prose surface from the server's stored
  history; tool-call traces remain available to the model.
- No new dependencies, no schema changes, easy to remove if a
  design pivot demands it.
- The pattern is reusable as a template for any other cross-turn
  cache that fits the same single-worker / per-session shape.

### Negative

- **Charts cannot be rehydrated.** The Plotly figure artefact
  never reached the conversation history; on reload the assistant
  bubble shows the textual `llm_response` confirmation only.
  Re-charting on demand is a Prompt-8-or-later concern.
- **Single-worker only.** Cross-worker state is process-local;
  scaling out requires Redis or equivalent. Same boundary as
  `pending_turns` and `_TURN_LOCK`.
- **Server restart loses every active conversation.** Users have
  to start over after a deploy. Acceptable at this phase, not at
  scale.
- **One thread per session.** No way to switch between named
  conversations, search past chats, or export. The next-strand
  multi-thread design will replace the single-`Conversation`
  store with a per-thread one.

### Migration Triggers

Three known triggers spell out when this design must be replaced:

1. **Multi-worker deployment.** Needs Redis or an equivalent
   process-external store; same trigger as `pending_turns` and
   `_TURN_LOCK`. All three caches migrate together — they share
   the same `app.state` precedent.
2. **Multi-thread conversation management** (named threads,
   switching, search, export). Needs Postgres persistence with
   a `chat_threads` + `chat_messages` schema and a repository
   layer. This is the most likely first trigger given the
   product direction.
3. **Cross-device session continuity.** Needs Postgres as well —
   a logged-in user opening the app on a different device
   expects their conversation history to follow.

All three triggers point at the same architectural step
(Postgres + Redis); designing for it now would be premature
abstraction. The single-line `chat_histories.pop(session_id, None)`
in logout, the bounded LRU, and the `_drop_history` helper are
the seams the migration replaces.

## Implementation Notes

- Modified: `web/routes/chat.py` — history store, history-aware
  SSE handler, `_TurnRecorder`, `_trim_history`, `POST /chat/new`,
  `GET /chat/history`.
- Modified: `services/ai_models.py` — `Message.tool_call_id` and
  extended `Conversation.to_openai_messages()` to serialise
  `tool_calls` (for assistant messages) and `tool_call_id` (for
  tool messages). Without this, replayed tool messages would
  400 the OpenAI API.
- Modified: `web/templates/chat.html` — `hx-trigger="load"` on
  `#chat-history`, "New chat" button.
- New: `web/templates/partials/chat_empty.html`,
  `web/templates/partials/chat_history.html`.
- Modified: `web/static/css/components/chat.css` — minor flex /
  positioning for the new button.
- Modified: `docs/Shirley_ToolOrchestration_Context.md` —
  "One turn at a time" → "Continuing across turns".
- Modified: `web/routes/login.py` — logout drops the session's
  history.
- Not modified: `services/ai_service_core.py`,
  `services/tools/_tool_context.py`, every tool module,
  `docs/Soul_Shirley.md`, the SSE event vocabulary, the tool
  registry.

## References

- Related ADRs:
  - ADR-0031 (`_TURN_LOCK`) — same single-worker boundary,
    same migration trigger shape.
  - ADR-0047 (tool-execution context propagation) — the
    per-turn context this strand explicitly does not extend
    to multi-turn handles.
  - ADR-0048 (two-axis chart architecture) — explains why
    charts are not rehydrated: the artefact is stripped before
    the LLM-bound tool result reaches the conversation.
  - ADR-0049 (tool orchestration in runtime context) — the
    "One turn at a time" paragraph this strand rewrites.

---

## Revision History

| Date       | Author                     | Change                   |
|------------|----------------------------|--------------------------|
| 2026-05-15 | PortfoliFLOW project owner | Initial draft (Accepted) |
| 2026-05-20 | PortfoliFLOW project owner | Maintainer name anonymised per project convention. |
