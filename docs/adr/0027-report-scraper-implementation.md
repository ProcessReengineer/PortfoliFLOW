# ADR-0027: Report Scraper Implementation

- **Status:** Accepted
- **Date:** 2026-04-27
- **Deciders:** PortfoliFLOW project owner
- **Tags:** integration, architecture, data, ui

---

## Context

PortfoliFLOW receives GP quarterly reports as heterogeneous PDFs (occasionally
Excel files). CLAUDE.md and ADR-0020 listed the **Report Scraper** as a planned
module — bulk ingestion, AI-powered extraction of key metrics and management
commentary, with full source attribution (document name, page number).

This ADR records the implementation decisions actually taken; the capability
listed as "planned" in CLAUDE.md is now built.

A terminology note up front, taken from CLAUDE.md and reproduced here so this
ADR can be read standalone: PortfoliFLOW uses the word *scraper* for two
distinct capabilities. The **News Scraper** (`services/web_research/`,
ADR-0023 / ADR-0024) fetches public press coverage via RSS feeds for
Shirley's Web Research tool. The **Report Scraper** documented here
(`modules/assistants/report_scraper.py` + `services/scraper/`) extracts
structured metrics from GP quarterly reports uploaded by the user. The two
share no code path and serve different user workflows.

The decision is integration-relevant (a new external-data ingestion path),
audit-relevant (every extracted finding must be attributable to a page in a
document), and AI-architecture-relevant (an LLM call is the extraction
engine, routed through the AIService seam of ADR-0010).

## Decision

PortfoliFLOW ships a Report Scraper Feature with the shape described below.
DataVault persistence (ADR-0017) is **not** yet integrated; the scraper's
output is currently surfaced in the GUI for the user to inspect, with
persistence deferred to the DataVault implementation.

**Module shell.** `modules/assistants/report_scraper.py` defines
`ReportScraperModule` (subclass of `BaseModule`,
`module_area="assistants"`, `module_name="report_scraper"`), registered via
`@registry.register`. The module is a thin shell — it holds a
`services.scraper.service.ScraperService` instance as `self.service` so
the GUI widget can call extraction directly without going through `run()`.
This mirrors the same thin-shell pattern used by `Shirley`. The
programmatic `run()` entry point exists for non-GUI callers (test
harnesses, batch scripts) and validates `attachments`, `keywords`, and
`model` arguments before delegating to the service.

**Pure-Python backend.** All extraction logic lives under
`services/scraper/` and is decoupled from PyQt6:

- `services/scraper/models.py` — plain frozen / mutable dataclasses:
  `Keyword(name, type)` with `KeywordType ∈ {NUMBER, PERCENTAGE, DATE,
  TEXT, LIST}`; `Attachment(filename, mime_type, data)` where `data` is
  `bytes | str` (binary attachment, or pre-extracted text for formats no
  current LLM understands natively); `Finding(keyword, value, source,
  confidence)` with `Confidence ∈ {HIGH, MEDIUM, LOW, NOT_FOUND}`;
  `ReportExtraction(filename, fund_name, period, findings, error)`;
  `ScraperResult(extractions, cancelled)`. The `Attachment.data` union
  is deliberately reusable by the upcoming DD Support Feature.
- `services/scraper/capabilities.py` — model capability lookup keyed on
  fnmatch patterns. The capability map lives in
  `config/scraper_model_capabilities.json` and records, per pattern,
  the content-block format (e.g. `anthropic_document`), the maximum
  PDF size in megabytes, and an informational maximum page count. An
  unknown model raises `UnsupportedModelError` rather than falling back
  silently — the silent fallback was the failure shape the design most
  wanted to avoid (OpenRouter sometimes strips malformed content parts
  rather than rejecting the request).
- `services/scraper/json_parser.py` — defensive three-tier JSON
  parser. Tier 1: ```` ```json ```` fenced block (the happy path).
  Tier 2: any ```` ``` ```` fenced block, parsed as JSON. Tier 3:
  first `{` to last `}` in the entire response. Tier 2 and Tier 3
  log a WARNING so the developer can monitor how often the fallbacks
  trigger; if all three fail, the parser raises `JsonParseError` with
  a 200-character preview of the response.
- `services/scraper/service.py` — the orchestrator (`ScraperService`)
  driven by the GUI widget worker.
- `services/scraper/message_builder.py` — assembles the per-model
  message payload using the capability lookup.

**Extraction prompt.** The system prompt that drives the LLM extraction
lives in `docs/Scraper_Prompt.md` between triple-backtick fences — a
reviewable artefact, edited deliberately because every change alters
extraction behaviour. The prompt instructs the model to return, for each
requested keyword, an object with `value`, `source`, and `confidence`
plus two top-level fields `fund_name` and `period`. Confidence is
strictly `High`, `Medium`, `Low`, or `Not_Found`. Type formatting is
specified per `KeywordType` (Number, Percentage, Date, Text, List).

**LLM routing.** All LLM calls go through `AIService` (ADR-0010). The
Report Scraper does not instantiate an OpenAI / Anthropic client itself,
does not bypass the AIService credential surface, and does not use the
ToolRegistry — it is **not** registered as a tool callable by the agent;
it is invoked directly from the GUI / module path on user action.

**GUI.** `gui/widgets/report_scraper_widget.py` provides the user
interface — file picker for attachments, keyword editor, model picker
(reading the capability map for the available choices), progress UI
(driven by the `progress_callback` and `cancel_check` hooks the service
exposes), and a results view that surfaces each finding with its
confidence indicator and source string.

This ADR is `Accepted` because the implementation described above is in
the working tree and in use.

## Rationale

- **Strict JSON schema with confidence levels makes findings reviewable.**
  The institutional bar for AI-extracted data is human review before the
  data is trusted; a free-form summary is unreviewable at scale. The
  per-finding `confidence` slot lets the user triage a hundred findings
  in minutes.
- **Source attribution is non-negotiable.** Every finding includes a
  `source` string (e.g. "Page 12, Cashflow Statement"). The prompt
  requires it; the dataclass requires it. A finding without a page
  reference cannot be reconciled against the underlying document, and
  audits will ask for the page reference.
- **Pure-Python backend is separable from the GUI.** The same scraping
  logic must be reusable later (batch ingestion, scheduled scrapes,
  Heartbeat-style agent invocation). Putting it under `services/scraper/`
  rather than inside the module keeps the boundary clean.
- **AIService routing is the rule, not an option.** ADR-0010 says no
  module instantiates its own LLM client. The Report Scraper observes
  this rule.
- **Silent capability fallback is forbidden.** A model whose PDF-input
  format is unknown must surface as a clear error in the UI, not as a
  silently malformed request. `UnsupportedModelError` is the explicit
  failure mode.
- **Three-tier JSON parser is a pragmatic concession to LLM reality.**
  The happy path is a `json` fenced block; the two fallbacks recover
  from common upstream artefacts (missing language hint, prose
  preamble) without giving up. The WARNING-level logging on Tier 2 /
  Tier 3 keeps the developer informed when reliability degrades.

## Alternatives Considered

- **Regex / template-based extraction without LLM.** Rejected. GP
  reports vary widely in layout; building per-GP templates would cost
  more than running an LLM extraction with human review. The
  variability is the point — the LLM is the right tool for it.
- **Free-form LLM output with post-hoc parsing.** Rejected. Findings
  without `confidence` and `source` are unreviewable at scale; an LP
  audit cannot cope with prose.
- **Direct DataVault persistence in this iteration.** Rejected. The
  DataVault (ADR-0017) does not yet exist; persistence is deferred
  until it does. In the interim, output is surfaced in the GUI for
  the user to inspect.
- **Register as an AI-callable tool.** Rejected. Extraction is
  user-driven (the user selects files, selects keywords, picks a
  model, presses a button). Exposing it as a tool callable by the
  agent would invite cross-context invocations whose authority and
  rate-limit story is undefined. If a future Feature needs Shirley
  to invoke extraction, the right shape will be a separate, narrower
  tool with its own ADR.
- **Single-tier JSON parser (json-fence only).** Rejected. Empirically,
  upstream LLM outputs occasionally drop the language hint or wrap
  the JSON in unrelated prose. A single-tier parser would surface
  those as user-visible errors; the three-tier parser turns them into
  a logged warning and a successful extraction.

## Consequences

### Positive

- GP reports can be processed today.
- Findings are auditable: every value carries a confidence and a
  source string; the schema and prompt are reviewable artefacts.
- The pure-Python backend is callable from a future batch path or
  agent invocation without code edits.
- `Attachment.data: bytes | str` makes the scraper trivially reusable
  by the upcoming DD Support Feature, which will pre-extract Excel
  content into text before sending.

### Negative

- No persistence in this iteration. Re-running the same scrape
  re-incurs the LLM cost; findings are not yet linked to a fund or
  period in any persistent store.
- Bulk-processing patterns (parallel extraction across many files,
  resume-on-failure, queueing) are not yet defined.
- Extraction quality depends on the LLM and on the user's keyword
  choices. Low-quality keywords yield low-confidence findings.
- The capability map is a small operational surface: when a new model
  is wanted, an entry must be added and verified against an actual
  document.

### Neutral / Follow-ups

- **Human-review workflow before findings are accepted into the
  DataVault** — flagged as gap #10 in `docs/adr/0000-retrofit-report.md`
  and intentionally left for a future ADR. Phase 1 surfaces findings
  for inspection; the policy that turns "inspected" into "accepted into
  the DataVault" belongs to the DataVault implementation.
- **Bulk / scheduled extraction** — out of scope here; expected to
  reuse the same `ScraperService.scrape_reports` entry point.
- **Capability-map drift** — adding a new model requires a verified
  entry; treat capability-map edits as audit-relevant and review them
  on commit.

## Implementation Notes

- Module: `modules/assistants/report_scraper.py`,
  `modules/assistants/__init__.py` (registration import).
- Backend: `services/scraper/` (`__init__.py`, `service.py`,
  `models.py`, `json_parser.py`, `capabilities.py`,
  `message_builder.py`).
- Capability map: `config/scraper_model_capabilities.json`.
- System prompt: `docs/Scraper_Prompt.md` (reviewable artefact).
- GUI: `gui/widgets/report_scraper_widget.py`.
- Tests: `tests/assistants/test_scraper_widget.py`, plus per-component
  tests under `tests/services/scraper/`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Functional Suitability
  (delivers GP-report extraction), Reliability (confidence levels are
  part of the contract; the three-tier JSON parser absorbs upstream
  LLM-format jitter without crashing), Security (LLM call routed
  through AIService — no separate credential surface), Maintainability
  (pure-Python backend, GUI shell separable, capability lookup
  externalised to JSON).
- **Regulatory references:** BAIT AT 7.2 (IT-risk management — the
  capability map and the Scraper prompt together form the documented
  control surface for the extraction path; an unknown model fails
  loudly rather than silently).
- **Audit evidence:** `docs/Scraper_Prompt.md` (the LLM's instructions);
  the dataclasses in `services/scraper/models.py` (the schema for what
  may be returned and stored); `config/scraper_model_capabilities.json`
  (the explicit list of supported models with their per-model limits);
  the GUI confidence indicators in
  `gui/widgets/report_scraper_widget.py` (the human-review surface);
  the tier-2 / tier-3 WARNING logs from `json_parser.py` (the
  observability surface for parser-fallback drift).

## References

- ADR-0010 (AIService — used for extraction)
- ADR-0012 (ToolRegistry — Report Scraper does **not** register a
  tool; it is user-driven and invoked directly from the GUI / module
  path)
- ADR-0017 (Planned DataVault — destination once implemented; this
  ADR's persistence gap closes there)
- ADR-0020 (Reporting Engine — sibling planned Feature; ADR-0026
  documents its Phase-1 delivery)
- ADR-0023 / ADR-0024 (News Scraper / Web Research — distinct
  capability; named here only to keep the *News Scraper* vs *Report
  Scraper* terminology unambiguous, per CLAUDE.md)
- Cross-reference to gap #10 of
  `docs/adr/0000-retrofit-report.md` for the human-review-policy
  follow-up.

---

## Revision History

| Date       | Author                       | Change        |
|------------|------------------------------|---------------|
| 2026-04-27 | PortfoliFLOW project owner   | Initial draft. Records the implementation under `modules/assistants/report_scraper.py` and `services/scraper/`. Code already implemented and in use; DataVault persistence remains deferred to ADR-0017. |
