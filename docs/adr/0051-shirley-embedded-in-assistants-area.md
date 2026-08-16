# ADR-0051: Shirley Embedded in the Assistants Area; `/chat` Retired

- **Status:** Accepted
- **Date:** 2026-05-15
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, integration, ui

---

## Context

Phase 6 Block 1 (ADR-0046) introduced the five-area sidebar and a
canonical `<area>/<section>` information architecture. Every other
operator-facing surface moved into a section under its owning area
during sub-streams 6F-1 … 6F-6: Charts, Statistics, Portfolio
Analysis, Portfolio Review, SAA, Data Import. The chat surface was
the exception. It kept its standalone `GET /chat` URL, and the
`shirley` section under `/assistants` was a placeholder that pointed
operators back at that URL.

By the time Prompt 7 / ADR-0050 (multi-turn history) and Prompts
4 – 6b (the two-axis chart pipeline, ADR-0048) landed, the chat
surface was functionally complete: history hydrates on page load,
multi-turn references resolve, `render_chart` returns interactive
Plotly figures, the Postgres-native investment tools execute under
the tool-execution context (ADR-0047). The remaining gap was IA: two
canonical paths to the same widget meant double the templates,
double the navigation logic, and a divergence risk every time the
chat shell evolved.

The application is not yet in production. Bookmarks to `/chat`
exist only on developer machines. The cost of retiring the URL is
the user retraining themselves to click the Assistants sidebar entry
once; the cost of keeping it is permanent two-surface maintenance
plus the IA inconsistency.

## Decision

Retire `GET /chat`. Lift the chat shell into the Assistants area's
`shirley` section via the existing `section_body_template` pattern
(`_partials/areas/_section.html`). The standalone template
`web/templates/chat.html` is deleted. The Assistants area handler
threads `model_id` into the section context so the embedded shell
can render the "Model: …" status line previously emitted by
`chat.html`.

The chat **backend endpoints** (`POST /chat/messages`,
`GET /chat/stream/<turn_id>`, `POST /chat/new`, `GET /chat/history`)
keep their `/chat/...` paths and their existing wire shape. The
embedded shell consumes them from the new mount point unchanged —
HTMX targets the same `#chat-history` div, the SSE handler is
identical, the per-session history store still keys on the session
UUID. A rename to `/api/assistants/shirley/...` would be churn for
no benefit at this point and is not in this ADR.

The standalone page's `<header class="chat-header">` block — title,
model status, "New chat" button — is replaced by:

* the area section's own `<h2>` (rendered by `_section.html`) as
  the heading,
* a `<p class="chat-embed__model">` status line above the history
  pane, conditional on `model_id`,
* a small `<div class="chat-controls">` row between the history and
  the composer holding the "New chat" button. This places the
  control next to the input where the operator's attention is, not
  in a remote header bar.

CSS rules for `.chat-page` / `.chat-header__*` are removed; new
`.chat-embed` / `.chat-embed__model` / `.chat-controls*` rules
replace them. The bubble / composer / chart rules are untouched —
they were never page-level concerns.

The Assistants-area `ai-settings` placeholder tile keeps its slot
but now points at the live surface introduced by ADR-0052:
*"AI provider configuration lives under [Admin → AI Settings](
/admin#ai-settings)."*

## Consequences

**Positive.**

- One canonical URL for Shirley: `/assistants#shirley`. No
  two-surface drift between `chat.html` and `_shirley_section.html`.
- The sidebar IA is consistent — every operator-facing surface
  lives under an Area / Section path.
- The embedded shell inherits the area shell's navigation, status
  bar, and section indicator for free.
- The HTMX area-switch path (`HX-Request: true`) now exercises the
  chat surface end-to-end, including hydration, since
  `chat_history`'s `hx-trigger="load"` fires on the swapped
  fragment too.

**Negative / acceptable.**

- Bookmarks to `/chat` 404. Acceptable because the application is
  pre-production; the test suite covers the 404 explicitly
  (`tests/web/test_chat_page_removed.py`) so the removal cannot
  silently regress.
- `chat.css` and `chat.js` now load globally from `base.html`
  rather than per-page. Both are small (< 10 kB combined,
  uncompressed) and the JS guards every listener on element
  presence; loading them on Front Office or Back Office costs
  nothing functionally. This trades a small static-asset cost for
  zero-fragility HTMX area switches into `/assistants` from any
  starting area.
- The backend endpoint paths keep the `/chat/` prefix even though
  the page that owned them is gone. This is a deliberate decision
  to avoid churn: the embedded shell, `chat.js`, the test suite
  and every ADR cross-reference would otherwise need a rename.
  A future prompt may rename to `/api/assistants/shirley/...`
  when the churn is worth it.

## Cross-references

- **ADR-0046** — established the area sidebar IA that this ADR
  brings the chat surface into compliance with.
- **ADR-0048** — two-axis chart architecture; consumed unchanged
  by the embedded shell.
- **ADR-0050** — multi-turn history; the precondition that made
  the embedded shell feature-complete enough to retire `/chat`.
- **ADR-0052** — runtime AI Settings under Admin; replaces the
  Assistants-area placeholder tile that this ADR repoints.

---

## Revision History

| Date       | Author                     | Change                                              |
|------------|----------------------------|-----------------------------------------------------|
| 2026-05-20 | PortfoliFLOW project owner | Maintainer name anonymised per project convention. |
