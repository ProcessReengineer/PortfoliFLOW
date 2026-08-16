# ADR-0053: Report Scraper Web Surface under `/assistants#report-scraper`

- **Status:** Accepted
- **Date:** 2026-05-15
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, integration, ui

---

## Context

The Report Scraper service (`services/scraper/`, ADR-0027) is
functionally complete and PyQt-free. `ScraperService.scrape_reports`
accepts a list of `Attachment` (filename + bytes), a list of
`Keyword` (name + type), a model id, and optional
`progress_callback` / `cancel_check` hooks; it returns a
`ScraperResult` with one `ReportExtraction` per file. Per-file
errors are captured into `ReportExtraction.error` without aborting
the run; run-wide preconditions (unsupported model, missing prompt
file) raise before any file is processed.

The Phase-6 Assistants embedding (ADR-0051) brought Shirley into
the area shell and gave the AI Settings surface a runtime-editable
home (ADR-0052). The Report Scraper section under `/assistants`
remained a `planned` placeholder — the service was reachable from
nowhere on the web side. The PyQt6 widget was never built; the web
surface is the first operator entry point.

What is missing is the surface: PDF upload, a keyword editor, a
model dropdown, an Extract button, a progress stream during the
run, and a results display when it finishes. Two design questions
shape the decision:

1. **How to drive the synchronous service from the asyncio loop**
   without freezing the uvicorn worker. The chat surface's
   `_TURN_LOCK` work (Prompt 5, ADR-0031) is the precedent: hand
   the blocking call to `asyncio.to_thread` and bridge the
   `progress_callback` thread → asyncio queue with
   `loop.call_soon_threadsafe`.
2. **Where the result lives** between completion and the
   operator's next workflow step. The data model for cross-quarter
   diffing and per-investment association is not yet settled; the
   service produces a `ScraperResult` envelope that does not map
   cleanly onto the `investment_navs` / `investment_cashflows`
   tables today.

## Decision

A single area section under `/assistants#report-scraper`:

- **Form** — multipart upload (one or more PDFs), a dynamic
  keyword-editor fieldset, a model dropdown sourced from the
  intersection of the AI Settings allowlist
  (`web.routes.ai_settings._MODEL_ALLOWLIST`) and the capability
  map (`config/scraper_model_capabilities.json`), and an
  operator hint stating "Only Anthropic models currently support
  PDF extraction". When the intersection is empty the form is
  replaced by a notice pointing at the capability map and
  `services/scraper/message_builder.py`.
- **Default keyword set** — eight entries chosen for institutional
  fund-of-funds use (Fund Name, Reporting Period, NAV, TVPI, DPI,
  Net IRR, Capital Called, Capital Distributed). Rendered into the
  form on first section load; the operator edits the rows before
  submitting. No save/load of named keyword sets in this iteration.
- **Submission shape** — multipart/form-data with `pdf[]`, `model`,
  `csrf_token`, and a single `keywords_json` hidden field
  serialised by `scraper.js` on submit. The repeated-file shape is
  natural for `list[UploadFile]`; the JSON-encoded keywords shape
  is natural for the dynamic keyword editor (one `onsubmit`
  serialisation, instead of two parallel form-field lists zipped by
  index on the server). Picked Option B from the design
  alternatives.
- **Drive shape** — `POST /scraper/runs` validates, reads the
  upload bodies into memory, stashes a `_PendingRun` on
  `app.state.scraper_runs`, returns an SSE-mount fragment.
  `GET /scraper/runs/<id>/stream` opens the SSE stream and
  invokes the service via `asyncio.to_thread`. A bridging
  `asyncio.Queue` forwards `progress` events from the worker
  thread to the SSE generator via `loop.call_soon_threadsafe`.
  `POST /scraper/runs/<id>/cancel` sets the run's
  `threading.Event` flag; the service polls it via `cancel_check`.
- **Wire format** — SSE events: `progress` (rendered progress
  partial), `result` (rendered results partial), `cancelled`
  (rendered partial + cancelled flag), `error` (message text). The
  stream closes on every terminal event; no reconnection.
- **In-memory run store** — bounded LRU at 32 entries on
  `app.state.scraper_runs`, keyed by `run_id`, session-scoped via
  the `session_id` field. Same single-worker contract as
  `chat_histories` and `pending_turns`; multi-worker deployments
  need Redis or equivalent (same migration trigger as ADR-0050).
- **No persistence.** The `ScraperResult` lives in memory for the
  run's duration and is rendered inline on completion. The
  operator captures the output out-of-band (clipboard / screenshot
  / future CSV download).
- **Logout integration.** The logout handler cancels and drops the
  session's in-flight runs, mirroring the chat-history drop from
  ADR-0050.

The Assistants area's `report-scraper` placeholder is replaced by
a lazy-loaded section body
(`_partials/scraper_section_lazy.html` → `GET /scraper/section`)
following the same pattern as the Charts section. Lazy loading
avoids paying for the capability-map read on every Assistants
area render.

## Consequences

**Positive.**

- The operator can run quarterly extractions immediately. The
  surface size is bounded: no schema migration, no result history,
  no investment association — only the upload form, the stream,
  and the result render.
- `services/scraper/` is consumed unchanged. The Qt-free service
  invariant survives intact; no UI logic leaks into the service
  layer.
- The `asyncio.to_thread` + `call_soon_threadsafe` bridge follows
  the precedent ADR-0031 set for blocking-from-asyncio calls. The
  uvicorn loop stays responsive — a 60-second scrape on three
  PDFs does not block the chat surface in the sibling section.
- The capability-map intersection is the single source of truth
  for the dropdown. A model added to the AI Settings allowlist that
  is not PDF-capable is silently excluded from the Scraper
  surface; a model that becomes PDF-capable shows up the moment
  the capability map is extended.
- Session isolation works out of the box: the run store is
  keyed by `run_id` but every read checks `session_id ==
  str(session.id)`, so cross-session stream and cancel attempts
  are 404s and silent no-ops respectively.

**Negative / acceptable.**

- **No result persistence.** Reloading the tab cancels the run
  (the SSE connection drops, the cancel hook fires). This matches
  the chat surface (Prompt 7) and keeps the contract simple, but
  it means the operator must keep the tab open until completion
  and capture the result before moving on.
- **No quarter-over-quarter comparison.** Diffing across runs
  needs a result schema with an investment FK, which we do not
  have. Migration trigger: when the operator workflow stabilises
  ("scrape once, paste into Investment NAV editor" vs "scrape
  every quarter, diff against last quarter's findings"), the
  persistence shape follows the workflow's needs.
- **Anthropic-only model support today.** The capability map ships
  Anthropic-only (`anthropic/claude-*`); OpenAI and Google PDF
  input requires a separate change to
  `services/scraper/message_builder.py` and the wire format.
  Surfaced inline below the dropdown so the operator knows where
  to look.
- **Process-global run store.** Multi-worker deployments multiply
  the store: each worker's `app.state.scraper_runs` diverges. The
  same Redis-or-equivalent migration trigger that ADR-0050 flags
  applies. Acceptable today because `portfoliflow-web` is a
  single-worker deployment.
- **In-memory upload buffering.** Every uploaded PDF is read into
  a single `bytes` object. A defence-in-depth cap (64 MB
  combined) sits in `POST /scraper/runs`; the capability map's
  per-file MB limit then applies inside the service. Streaming
  uploads to disk would change the service's contract
  (`Attachment.data` is `bytes | str`) and is deferred.
- **No keyword-set save/load.** Each run starts from the default
  set; the operator re-edits as needed. Saving sets is a clear
  UI-polish line item for a later strand.

## Cross-references

- **ADR-0027** — Report Scraper service implementation. Consumed
  unchanged.
- **ADR-0031** — Module-level threading lock for blocking calls
  from asyncio. The `asyncio.to_thread` + `call_soon_threadsafe`
  bridge here applies the same lesson.
- **ADR-0050** — Multi-turn chat history in memory. Same
  bounded-LRU shape, same single-worker contract, same Redis
  migration trigger; the logout handler now drops both stores in
  lockstep.
- **ADR-0051** — Shirley embedded in the Assistants area. The
  Report Scraper section is the second live section under
  `/assistants`, following the same lazy-load + `section_body_template`
  pattern.
- **ADR-0052** — AI Settings runtime under `/admin`. The Scraper
  dropdown is sourced from the intersection of the AI Settings
  allowlist and the capability map, so a model change in AI
  Settings is reflected in the Scraper surface on the next render.

---

## Revision History

| Date       | Author                     | Change                                              |
|------------|----------------------------|-----------------------------------------------------|
| 2026-05-20 | PortfoliFLOW project owner | Maintainer name anonymised per project convention. |
| 2026-08-03 | PortfoliFLOW project owner | **Allowlist reference relocated.** ADR-0112 §6 (strand F3) retired the ADR-0052 AI Settings surface, so the model allowlist this ADR's §Form and §Cross-references cite as `web.routes.ai_settings._MODEL_ALLOWLIST` moved verbatim to `web.routes.scraper._MODEL_ALLOWLIST` — the Scraper is its only remaining consumer. The dropdown's contents are unchanged (still the intersection of that allowlist with `config/scraper_model_capabilities.json`). What no longer holds is the coupling: there is no AI Settings model dropdown to change any more, and the replacement surface treats a model name as free text, so the allowlist is now the Scraper's own constant rather than a shared one. |
