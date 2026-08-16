# ADR-0114: Chart Snapshot Persistence — Session Rehydration and Case Pinning

- **Status:** Accepted
- **Date:** 2026-08-05
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, ui, cases, shirley, persistence, audit

---

## Context

Shirley's web charts do not survive navigation. `render_chart`
(ADR-0048) emits a themed Plotly figure spec that reaches the browser
as an SSE `chart` event and is rendered client-side via
`Plotly.newPlot`. The spec is deliberately stripped before the
LLM-bound tool message, and it never enters the session's chat history
(ADR-0050). Consequently:

- **Chat surface:** switching away from the Assistants area and back —
  or reloading the tab — triggers `GET /chat/history`, which restores
  the prose bubbles only. Every chart of the session is gone. ADR-0050
  records this openly ("Charts cannot be rehydrated") and defers the
  fix to an artefact-rehydration strand.
- **Cases surface:** a PM who consults Shirley while working a case can
  pin a *text* excerpt to the case record (ADR-0107 C6,
  `artifact: "consultation"`), but not a chart. An analysis whose
  substance is a figure — a NAV trajectory, a cashflow profile —
  cannot reach the case record at all. Reopening the case shows the
  words around a chart that no longer exists.

The historical justification for not persisting charts — storage cost —
predates ADR-0048. It applied to the matplotlib-PNG era. Since the
two-axis migration, the web artefact is a JSON dict: a typical
per-investment NAV spec is tens of kilobytes and compresses well under
JSONB TOAST. The premise no longer holds.

A replay-based alternative (archive the tool call, re-execute on
rehydration) was considered during concept work and rejected; see
Alternatives.

## Decision

Charts are persisted as **frozen snapshots of the rendered Plotly
spec** — exactly what the user saw at generation time. There is no
re-execution, no recomputation, and no silent refresh, on any surface.
This extends the principle ADR-0107 C5 already binds for scenario
snapshots ("rendered verbatim from the payload, nothing recomputed")
to chart artefacts.

Two seams implement the principle, sharing one artefact format:

### 1. Chat: per-session chart-artefact sidecar

A per-session artefact store held **parallel to** the chat history of
ADR-0050 — *never inside it*. The conversation history remains the
LLM-bound record; the spec must not re-enter the model's token stream.

- **Store:** `app.state.chat_chart_artifacts` (working name), keyed by
  the authenticated session UUID like `chat_histories`, holding an
  ordered list of artefact records per session:
  `{artifact_id, message_id, spec, caption, created_at}` where
  `message_id` is the id of the assistant message the chart belongs to.
- **Lifecycle:** identical to the history store. Login/logout, "new
  chat", and the session-level LRU drop the sidecar together with the
  history. A server restart loses both — consistent with ADR-0050's
  contract for prose.
- **Trim coupling:** the turn-group trim of ADR-0050 evicts the
  artefacts belonging to an evicted group in the same operation.
  Orphaned specs must not accumulate.
- **Capture point:** the SSE handler in `web/routes/chat.py`, in the
  same `chart_artifact` branch that today forwards the spec to the
  browser — the spec is at hand there and nowhere else.
- **Rehydration:** `GET /chat/history` renders chart placeholders
  interleaved at their message positions; a small client-side
  initialiser feeds each stored spec to `Plotly.newPlot`, reusing the
  rendering path `chat.js` already has for the live SSE case. The
  restored figure is the initial view: zoom state and hidden traces
  are interaction state, not artefact content, and are not preserved.

### 2. Cases: fourth pin artefact class `chart_snapshot`

The consultation-pin flow (ADR-0107 C6) is extended with a chart pin:

- **Affordance:** the chart bubble gains a "Pin to case…" affordance
  analogous to the text bubbles'.
- **Transport by reference, never by value:** the pin dialog and POST
  carry the sidecar `artifact_id` only. The server resolves the spec
  from its own store. The client never serialises or posts the Plotly
  spec — the analog of C6 binding decision 2 (the client never scrapes
  bubble HTML). A stale or evicted `artifact_id` renders the calm
  "no longer available" state C6 already defines for trimmed messages.
- **Payload:** the journal entry is
  `kind="pin"`, `actor="pm"` (pinning is the PM's curation act), with
  `payload={"artifact": "chart_snapshot", "comment": <curation
  comment>, "caption": <chart caption>, "spec": <Plotly spec>}`.
  The spec is embedded in the entry payload: the case record is
  **self-contained**. No reference into a session store (ephemeral) or
  a shared results store (whose retention would then govern case
  integrity).
- **Rendering:** `cases_detail_timeline.html` renders the class with
  the pin anatomy (curation comment + artefact), the figure via
  `Plotly.newPlot` from the stored spec — verbatim, nothing recomputed,
  exactly as the `scenario_snapshot` class prescribes. Unknown-class
  fallback behaviour is unchanged.
- **Gates and immutability:** the C5/C6 gate order (comment → artefact
  present → case exists → case open) and closed-case immutability
  (ADR-0107 §4) apply unchanged.

### 3. Spec-size cap

A single guard at the single capture point: when the serialised spec
exceeds **1 MiB** (`_CHART_SPEC_BYTE_CAP`, a tunable constant), the
sidecar does not archive it. The live SSE stream is unaffected — the
user still sees the chart in the moment. Consequences of non-archival:

- Rehydration renders a calm placeholder ("This chart was too large to
  restore — ask Shirley again.").
- The pin affordance is unavailable for the unarchived chart; pinning
  an oversized figure is answered with guidance to narrow the range.

Rationale for the value: typical specs are 30–60 KB; only
`portfolio_nav_series` near its 200 k-row cap (ADR-0048 amendment) can
produce multi-megabyte specs, which are poor fits for a case record
anyway. The cap is a memory/storage guard in the `_DATA_ROW_CAP`
tradition, not a token-budget concern. Degrade, never refuse the live
render.

## Rationale

- **Audit semantics decide the design.** A case is a decision record
  (ADR-0107; BAIT/VAIT context). Reopening a case must show what the
  decision-maker saw at decision time. Any replay against live data
  makes the record non-reproducible over time and silently divergent.
  The frozen spec is the only artefact that satisfies this, and since
  ADR-0048 it is cheap.
- **The spec is self-contained.** `render_chart` bakes the theme into
  the spec at render time (`layout_from_theme`). Later theme changes
  do not alter archived charts — desirable for the audit reading.
- **Sidecar before pin.** The server must own the spec for the pin to
  be trustworthy; the session sidecar is that ownership. The two seams
  are one decision, not two.
- **Precedent over novelty.** `chart_snapshot` is the fourth pin
  artefact class after `document`, `scenario_snapshot`, and
  `consultation`, and reuses the C5 snapshot principle and the C6
  dialog/gate idiom. No new mechanism is introduced.
- **ADR-0050 stays intact.** The sidecar inherits the history store's
  lifecycle, bounds, and migration triggers verbatim. When migration
  trigger 2 (multi-thread Postgres persistence) fires, the sidecar
  becomes a JSONB column beside the messages and migrates with them.

## Alternatives Considered

- **Replay: archive the tool-call chain, re-execute on rehydration.**
  Rejected. (a) `render_chart` consumes a turn-scoped `data_handle`
  (ADR-0048) that is cleared in the SSE handler's `finally`; a replay
  engine would have to re-run `get_investment_data`, obtain a fresh
  handle, and substitute it into the recorded arguments — a bespoke
  orchestrator, not a stored call. (b) Replay against live data
  violates the case-record semantics above and breaks when an
  investment is renamed or deleted. (c) It costs DB queries on every
  rehydration. (d) The storage it saves is JSON that is cheap to keep.
- **Reference a shared analysis-results store (ADR-0071) instead of
  embedding the spec in the journal payload.** Rejected for this
  artefact. ADR-0071 remains a stub with open questions
  (trust provenance, retention) that chart snapshots do not need — a
  `render_chart` spec is `READ_INTERNAL` content, and coupling case
  integrity to a generic store's retention policy would raise deletion
  questions the self-contained payload never asks. ADR-0071 stays
  untouched for its actual use case (externally fetched, run-bound
  results).
- **Server-side PNG rendering.** Rejected: larger artefacts, loss of
  interactivity, and a new server-side rendering dependency (kaleido).
- **Preserving interaction state (zoom, hidden traces).** Rejected:
  interaction state is view state, not artefact content; the initial
  view is the shared, unambiguous reading.

## Consequences

### Positive

- Charts survive tab switches and reloads within a session; the
  Assistants surface stops feeling lossy.
- Chart-bearing analyses can reach case records, self-contained and
  frozen — the record shows what was seen.
- No new tables, no migration for the chat seam; the case seam reuses
  the existing journal-entry JSONB payload.
- The snapshot principle is now stated once and shared by
  `scenario_snapshot` and `chart_snapshot`.

### Negative

- Journal-entry payloads grow by tens of KB per chart pin. Accepted:
  bounded by the per-case attachment/pin discipline and the spec-size
  cap.
- Rendering fidelity of archived specs is tied to the pinned Plotly.js
  version (2.35.2, `base.html`, ADR-0042 §4). A future major upgrade
  could cosmetically alter old snapshots; data content is unaffected.
  To be noted in any Plotly upgrade decision.
- Session-scoped chart artefacts still die with the session (restart,
  logout) — same contract as prose, resolved only by ADR-0050
  migration trigger 2.

### Neutral / Follow-ups

- The `png` artefact branch (GUI path) is out of scope: only
  `chart_format == "plotly"` artefacts are archived. The defensive PNG
  branch streams live as today and is simply not captured.
- Telegram surface is out of scope (charts there follow the bot's own
  delivery model).
- When ADR-0050's Postgres migration lands, the sidecar migrates into
  the thread schema; no interim change needed.

## Implementation Notes (anticipated)

- `web/routes/chat.py` — sidecar store beside `chat_histories`;
  capture in the `chart_artifact` SSE branch; trim coupling in
  `_trim_history`'s group eviction; artefact interleaving in
  `GET /chat/history`; pin-dialog GET/POST for the chart class
  (reusing `_pin_dialog_context` idioms and the C5/C6 gate order);
  `_CHART_SPEC_BYTE_CAP`.
- `web/templates/_partials/chat_history.html` — chart placeholder
  rendering at message positions.
- `web/static/js/chat.js` — shared render helper for the live SSE path
  and the rehydration path (both feed `Plotly.newPlot`).
- `web/routes/cases.py` / `_partials/cases_detail_timeline.html` —
  `chart_snapshot` rendering with the pin anatomy; payload contract
  documented beside the existing three classes.
- Tests — sidecar lifecycle incl. trim eviction and logout;
  rehydration ordering; pin gates incl. stale-artefact state; timeline
  rendering; cap degradation on both surfaces
  (`tests/web/test_chat_history.py`, `tests/web/test_chat_sse.py`,
  `tests/web/test_cases_area.py` as homes).
- Not modified: `services/ai_service_core.py` (the strip before the
  LLM-bound message stays), `services/tools/chart_tools.py`,
  `services/tools/_tool_context.py`, the tool registry, the SSE event
  vocabulary.

## Compliance & Audit Relevance

- **Audit evidence:** case records become self-contained with respect
  to chart artefacts; reopening a case reproduces the decision-time
  view byte-identically (modulo the Plotly.js rendering engine).
  Nothing on the Cases surface recomputes.
- **ISO 25010:** Reliability (deterministic rehydration), Usability
  (no silent loss of artefacts), Maintainability (one snapshot
  principle shared across pin classes).

## References

- ADR-0048 (two-axis chart architecture; spec artefact, handle
  lifecycle, `_DATA_ROW_CAP`), ADR-0050 (in-memory history; the
  artefact-rehydration deferral this ADR resolves; migration
  triggers), ADR-0107 (Cases area; §4 immutability, §7 pin anatomy,
  C5 scenario snapshot, C6 consultation pin and its binding
  decisions), ADR-0042 §4 (Plotly.js pinning), ADR-0071 (analysis-
  results store — explicitly not used here).
- Roadmap: item to be assigned via roadmap governance (#056
  anticipated); sequenced before the AGPL public release (#052 family),
  covered by the pre-release hardening test.

---

## Revision History

| Date       | Author                     | Change                |
|------------|----------------------------|-----------------------|
| 2026-08-05 | PortfoliFLOW project owner | Initial draft (Proposed) |
| 2026-08-05 | PortfoliFLOW project owner | Accepted; registered in the index |
