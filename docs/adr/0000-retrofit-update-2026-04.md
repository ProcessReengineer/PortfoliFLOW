# ADR-0000 Update: Retrofit Follow-up — 2026-04-27

- **Status:** Informational (not a decision)
- **Date:** 2026-04-27
- **Author:** PortfoliFLOW project owner
- **Tags:** process, meta

---

## Summary

This is the first follow-up to the original retrofit report at
[`0000-retrofit-report.md`](./0000-retrofit-report.md), which on 2026-04-24
lifted twenty-one existing PortfoliFLOW decisions into ADR form (ADR-0001
through ADR-0021) and identified thirteen gaps as candidates for future
ADRs. Between 2026-04-24 and 2026-04-27, the project shipped four
additional architectural decisions whose code is now in the working tree
but whose ADRs had not yet been written. This update formalises those four
decisions, corrects the status of one previously-`Proposed` ADR whose
implementation has landed, and records Revision-History extensions on four
ADRs whose content has been extended without altering the underlying
decision.

The original retrofit report is **not** edited — its text continues to
describe the state of the codebase as of 2026-04-24. This update is the
authoritative record of what changed since.

## Method

Sources read in full before drafting (delta vs. the original retrofit
sources, which remain authoritative for ADR-0001 through ADR-0024):

- All implementation files for the new ADRs as enumerated in the
  prompt — `core/theme_service.py`, `core/ui_theme.py`, `gui/theme.py`,
  `gui/theme_persistence.py`, `config/ui_theme*.json`,
  `config/chart_theme*.json`, `services/reporting/*`,
  `services/scraper/*`, `services/web_research/*`,
  `services/tools/chart_tools.py`, `services/tools/__init__.py`,
  `modules/investor_communication/portfolio_review.py`,
  `modules/assistants/report_scraper.py`, `gui/widgets/portfolio_review_widget.py`,
  `gui/widgets/report_scraper_widget.py`, `docs/Scraper_Prompt.md`,
  `docs/Fetcher_Prompt.md`, `docs/Feed_Filter_Prompt.md`,
  `config/web_research.yaml`.
- `docs/adr/0012-*`, `0020-*`, `0021-*`, `0023-*`, `0024-*` to
  determine the precise scope of each Revision-History extension.
- `CLAUDE.md` ("Current project status", "Implemented Services
  Reference", "Planned Architecture (Not Yet Implemented)") to identify
  out-of-date passages.

As in the original retrofit, no architectural alternatives were
fabricated where they had not been considered: alternatives present in
each new ADR are marked as either evaluated or "implicitly rejected —
not formally evaluated", consistent with the convention introduced in
the original retrofit.

## New ADRs

The following four ADRs were authored in this update; each is `Accepted`
because the corresponding code is in the working tree and in use.

- **ADR-0025 — UI Theming System with Multi-Variant Support.** Records
  the introduction of `core/ui_theme.py`, `core/theme_service.py`,
  `gui/theme.py`, `gui/theme_persistence.py`, and the
  `config/ui_theme*.json` family. UI-level theming sits beside the
  chart-level theming of ADR-0021; both decisions remain Accepted and
  neither supersedes the other.
- **ADR-0026 — Phase-1 Reporting Engine — In-App Multi-Tile Rendering.**
  Records the deliberate Phase-1 implementation of investor reporting
  under `services/reporting/` plus `modules/investor_communication/portfolio_review.py`,
  rendered inside the application via `FigureCanvasQTAgg`, with no PDF
  or PPTX export and no per-client Style Layer. ADR-0020's three-layer
  long-term design is **not** superseded — it remains the target.
- **ADR-0027 — Report Scraper Implementation.** Records the actual
  implementation of the Report Scraper Feature
  (`modules/assistants/report_scraper.py` shell;
  `services/scraper/` pure-Python backend; `docs/Scraper_Prompt.md`
  system prompt; LLM call routed through AIService). DataVault
  persistence remains deferred to ADR-0017.
- **ADR-0028 — `generate_chart` Tool as `READ_INTERNAL`.** A short
  member-extension ADR recording that the `generate_chart` tool was
  added to the ToolRegistry under class `READ_INTERNAL`, joining the
  three DataStore-reading tools listed in ADR-0012.

## Existing ADRs touched

Status field and Revision History only — no body rewrites. Each ADR
gained exactly one new Revision History row dated 2026-04-27.

- **ADR-0012 (ToolRegistry as Single Seam).** Status unchanged
  (`Accepted`). Revision History row added: tool list extended to
  include `generate_chart` (ADR-0028) and `web_research` (ADR-0023 /
  ADR-0024); seam decision unchanged.
- **ADR-0020 (Planned Reporting Engine — Three-Layer).** Status
  unchanged (`Proposed`). Revision History row added: Phase-1
  implementation has been delivered separately as ADR-0026; the
  three-layer Data/Template/Style design with PDF/PPTX export remains
  the long-term target.
- **ADR-0021 (Chart Theming Externalised to JSON).** Status unchanged
  (`Accepted`). Revision History row added: variant files
  `config/chart_theme_light.json` and `config/chart_theme_print.json`
  now ship alongside `config/chart_theme.json`; the single-config
  decision is preserved in spirit (one active at a time, selected via
  the theme service introduced in ADR-0025).
- **ADR-0023 (Web Research Capability).** Status unchanged
  (`Accepted`). Revision History row added confirming that
  `docs/Fetcher_Prompt.md`, `docs/Feed_Filter_Prompt.md`, and the
  allowlist file at `config/web_research.yaml` now exist — i.e. the
  Implementation Notes' "to be authored / created" clauses have been
  satisfied.
- **ADR-0024 (RSS-based Source Resolution for Web Research).** Status
  changed from `Proposed` to `Accepted`. Revision History row added:
  RSS-based source resolution is implemented in `services/web_research/`
  (allowlist `feeds` field, Feed-Filter-LLM via
  `docs/Feed_Filter_Prompt.md`, pattern-based fallback removed).

## Cross-document changes

Per-prompt scope, two non-ADR files received small targeted edits:

- **`CLAUDE.md`** — appended new lines to the "Current project status"
  list for the four shipped subsystems (reporting, scraper, web
  research, UI theming, generate_chart tool, the two new modules);
  rewrote the Reporting Engine and Report Scraper bullets in the
  "Planned Architecture" section to reflect the Phase-1 / implemented
  status; added an `(ADR-0028)` reference next to `generate_chart` in
  the ToolRegistry "Current tools" bullet. The News Scraper / Report
  Scraper terminology note remains unchanged.
- **`docs/architecture.md`** — extended the `services/` layer
  responsibilities with ADR-pointing references to the implemented
  service families, and added a Cross-cutting-services bullet for the
  UI theming system pointing at ADR-0025.

No code under `core/`, `services/`, `modules/`, `gui/`, `analytics/`, or
`tests/` was modified in this update. Discrepancies found while reading
the code are documented in the assistant report alongside this update,
not fixed here.

## Re-evaluation of the original gaps

The original retrofit identified thirteen numbered gaps. Status as of
2026-04-27, gap-by-gap:

1. **Authentication strategy.** Still open. No design exists; the
   project remains single-user.
2. **Authorisation / RBAC model.** Still open. Particularly relevant
   for AI-tool access control as the tool registry continues to grow
   (ADR-0012's note carries forward).
3. **Secrets management.** Still open. AIService API keys remain in
   `QSettings`; no policy on encryption at rest, key rotation, or
   per-environment scoping.
4. **Logging retention and audit-trail design.** Still open. The
   Web Research capability (ADR-0023 / ADR-0024) and the Report
   Scraper (ADR-0027) both expand the action surface; an explicit
   audit-trail policy becomes more valuable, not less.
5. **Data retention and deletion.** Still open. Becomes urgent at
   DataVault implementation time (ADR-0017).
6. **Backup and disaster recovery.** Still open. Becomes urgent at
   DataVault implementation time (ADR-0017).
7. **Error reporting / telemetry policy.** Still open.
8. **Versioning and release strategy.** Still open.
9. **Licence choice.** Still open. `readme.md` continues to state
   "Proprietary — all rights reserved." with no LICENCE file.
10. **Input validation policy for externally-sourced data (scraped
    reports).** **Partially addressed.** ADR-0027 records that the
    Report Scraper is implemented with a strict JSON schema, per-finding
    confidence, and source attribution per finding; this is the
    *technical* shape of validation. The **human-review policy** that
    turns "inspected in the GUI" into "accepted into the DataVault"
    is still open and is intentionally cross-referenced from ADR-0027
    as a future ADR. Carry forward as a remaining gap.
11. **Reproducibility of analytics outputs.** Still open. ADR-0026
    explicitly flags that Phase-1 reports read from a non-persistent,
    non-audited data source; reproducibility weakness is now
    documented but not yet decided. Becomes addressable at DataVault
    implementation time.
12. **Dependency / supply-chain policy.** Still open. The implementation
    of ADR-0026 / ADR-0027 / ADR-0028 added matplotlib /
    feedparser / pydantic usage that was already in the dependency
    set; no new policy decisions were taken.
13. **Configuration UI vs. `.env` boundary.** **Partially addressed
    — adjacent shape, not the boundary itself.** ADR-0025 introduces
    runtime-editable theme selection persisted via `QSettings` in a
    user-facing settings widget, which is structurally similar to the
    "future admin configuration UI" envisaged in
    `docs/architecture.md`. The boundary between `.env`-managed
    config, `QSettings`-managed config, and runtime-editable config
    is not yet a formal decision; treat as still open.

### Carried-forward open gaps

The following gaps remain open as candidates for future ADRs (numbering
preserved for traceability with the original retrofit):

1. Authentication strategy.
2. Authorisation / RBAC model.
3. Secrets management.
4. Logging retention and audit-trail design.
5. Data retention and deletion.
6. Backup and disaster recovery.
7. Error reporting / telemetry policy.
8. Versioning and release strategy.
9. Licence choice.
10. **Human-review workflow for scraped findings before DataVault
    write** (the policy half of original gap #10).
11. Reproducibility of analytics outputs.
12. Dependency / supply-chain policy.
13. Configuration UI vs. `.env` boundary.

## Suggested follow-up ADRs (delta vs. the original retrofit)

The original retrofit's suggested follow-up list remains valid. Two new
items observed while reading the 2026-04-27 codebase are added below;
neither was decided in this update.

- **Human-review policy for AI-extracted findings before DataVault
  write.** Already cross-referenced from ADR-0027. Belongs alongside
  the DataVault implementation ADR.
- **Theme hot-reload policy.** ADR-0025's Phase B has no hot-reload —
  changing themes requires an application restart. A future ADR may
  decide whether to add hot-reload (re-applying QSS at runtime,
  re-rendering open charts) and what its constraints are.

The original retrofit's `Cross-document follow-ups` recommendation is
also still partially open — the targeted edits made in this update
(CLAUDE.md current-project-status, architecture.md service-layer
references) close part of it; cross-references from individual code
docstrings remain a pending hygiene task.

---

## Revision History

| Date       | Author                       | Change                  |
|------------|------------------------------|-------------------------|
| 2026-04-27 | PortfoliFLOW project owner   | Initial follow-up to `0000-retrofit-report.md`. Records the four new ADRs (0025–0028), the five existing ADRs touched (0012, 0020, 0021, 0023, 0024), and the re-evaluated gap list. |
