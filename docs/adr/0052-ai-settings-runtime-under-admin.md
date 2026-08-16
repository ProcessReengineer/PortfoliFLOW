# ADR-0052: AI Settings — Runtime-Editable Under `/admin`, Persistence Deferred

- **Status:** Accepted
- **Date:** 2026-05-15
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, integration, ui, configuration

---

## Context

Shirley reads her model from `SHIRLEY_MODEL` and her OpenRouter API
key from `OPENROUTER_API_KEY` at startup. The PyQt6 GUI exposes
these as an AI Settings widget under
`modules.assistants.ai_settings` (writes to `QSettings`); the web
variant has had no equivalent surface — operator changes required
editing `.env` and restarting the FastAPI process.

With Shirley folded into the Assistants area (ADR-0051), the
remaining IA gap is the AI Settings tile. Operator demos require
flipping the model live ("watch what happens with Haiku vs Opus")
without dropping the session. The .env-only workflow forces a
server restart, which throws away in-memory state (the per-session
chat history from ADR-0050, the per-app pending-turns LRU, the
auth sessions).

A user-settings table is the right home for per-user, per-tenant
configuration — but that table is **Multi-Tenant Block 2** work,
gated on the multi-tenant migration and the RBAC schema. Shipping
a database surface now risks two migrations (one now, one when
Block 2 lands) and locks in a single-tenant schema shape that the
multi-tenant rewrite will then need to undo.

## Decision

Introduce a slim AI Settings section under `/admin#ai-settings`
that mutates the running :class:`AIServiceCore` singleton via
`set_model()` and `configure()`. **No persistence** in this
iteration:

- The form lives at `_partials/ai_settings_section.html`.
- The route module lives at `web/routes/ai_settings.py` and
  exposes `POST /admin/ai-settings` plus the shared context loader
  `load_ai_settings_section_context()` consumed by
  `admin_view()`.
- A static allowlist of model IDs (snapshot of the OpenRouter
  catalogue) is rendered as a `<select>`. The active model, if not
  in the allowlist, is prepended so the dropdown never silently
  switches it on save. Dynamic discovery via
  `AIServiceCore.fetch_models` is deferred — a live call would
  couple Admin-page renders to OpenRouter's availability.
- The API key field is `<input type="password">`. The current key
  is **never** rendered back to the client, masked or otherwise —
  a masked `••••••••` placeholder is shown when the core is
  configured, the empty string `Not configured` otherwise. An
  empty submission leaves the key unchanged.
- A neutral banner above the form reads:
  *"Settings apply to the running process. For permanence across
  restarts, add the values to your `.env` file. Per-user
  persistence in the database arrives with Multi-Tenant Block 2."*
  Style as informational, not as a warning.
- HTMX swap shape: `hx-target="closest .ai-settings"` plus
  `hx-swap="outerHTML"` so a successful POST replaces the entire
  form-plus-banner block with a freshly rendered one carrying
  the current state, current banner, no stale form values.
- The section partial is **server-rendered on initial load** —
  there is intentionally no lazy GET endpoint. A plain refresh of
  `/admin` always reflects current singleton state without
  needing JavaScript.
- `OPENROUTER_BASE_URL` is **not** editable in this iteration. It
  stays an environment-time setting; the route reads it from
  `WebSettings.openrouter_base_url` when it reconfigures the core.

The Assistants-area `ai-settings` placeholder tile (which
previously read *"AI provider configuration — base URL, API key,
model selection. Mirrors the QT AI Settings widget."*) is
repointed to *"AI provider configuration lives under
[Admin → AI Settings](/admin#ai-settings)."* The Admin section
catalogue (`web/shell.py::_SECTIONS_BY_AREA`) gains an
`ai-settings` entry between `data-import` and
`application-settings` so the section indicator and command
palette pick it up.

## Consequences

**Positive.**

- The operator can flip models on the live surface mid-session.
  Demo-critical for the "AI as tool, operator as architect"
  positioning.
- A misconfigured `.env` (or a key rotation upstream) no longer
  requires a server restart — the operator pastes the new key
  into the form and the next chat turn picks it up.
- The runtime state of the singleton is finally legible without
  digging into logs. The "API key configured" / "No API key
  configured" hint mirrors what `POST /chat/messages` already
  uses to gate the 503.
- The form's "no echo back" rule keeps the submitted key out of
  the response HTML, the browser cache, and any future server-side
  template-render audit. The active key never crosses the wire
  going outward; only inward, in the password input.

**Negative / acceptable.**

- The setting is **process-global**: it affects every user of this
  uvicorn worker, not just the operator who clicked Save. With
  the current single-operator phase this is fine; the next
  operator (Block 2 onwards) sees the same model the previous
  operator selected. Explicitly flagged as the migration trigger
  for Multi-Tenant Block 2 (the user-settings table absorbs
  this configuration).
- Multi-worker deployments multiply this: each worker's singleton
  state diverges. Acceptable today because `portfoliflow-web` is
  a single-worker deployment; the Redis-or-equivalent that ADR-0050
  flags for shared per-session state would also need to absorb
  the singleton config when multi-worker becomes real.
- No audit trail of who changed what when. Acceptable today (one
  operator), tracked by Block 2 along with the user-settings
  table.
- The static allowlist drifts. The catalogue snapshot date is
  pinned in the route module's docstring; refresh when the
  operator hits a model not in the list.

## Cross-references

- **ADR-0038** — AIServiceCore Qt-free design. This prompt consumes
  the `configure` / `set_model` / `set_status` API documented
  there exactly as the lifespan handler in `web/main.py` does.
- **ADR-0049** — Shirley tool-orchestration context. Independent
  axis; this ADR does not touch system-prompt or tool-context
  composition.
- **ADR-0051** — Shirley embedded under `/assistants`. This ADR
  fills the Admin-side counterpart of that move: the AI
  configuration surface that the placeholder tile in
  `_assistants_body.html` now redirects to.
- **Multi-Tenant Block 2** (no ADR yet) — user-settings table is
  the canonical absorbing destination for the values mutated here.

---

## Revision History

| Date       | Author                     | Change                                              |
|------------|----------------------------|-----------------------------------------------------|
| 2026-05-20 | PortfoliFLOW project owner | Maintainer name anonymised per project convention. |
| 2026-08-03 | PortfoliFLOW project owner | **Surface retired** by ADR-0112 §6 (strand F3). `/admin#ai-settings` is replaced by the **Providers & Credentials** module, which writes `scoped_settings` tenant rows (encrypted through `services/credential_vault`) instead of mutating the running `AIServiceCore` singleton. The banner's stated condition — *"the canonical persistence path is the `.env` file until per-user settings land"* — is met: user-scope rows land with the same surface, application scope stays the environment (ADR-0112 §1). `web/routes/ai_settings.py`, `_partials/ai_settings_section.html`, `components/ai_settings.css` and `tests/web/test_ai_settings.py` are deleted; `_MODEL_ALLOWLIST` moved to `web/routes/scraper.py`, its only remaining consumer, and is no longer offered as a dropdown (the taxonomy validates the key, not the value — value validation belongs to the consumer, ADR-0112 §4). The Assistants-area pointer tile survives, retargeted at `/admin#providers-credentials`. This ADR stays **Accepted** as the historical record of the runtime-mutation phase. |
