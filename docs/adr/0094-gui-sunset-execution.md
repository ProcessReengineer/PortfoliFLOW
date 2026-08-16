# ADR-0094: GUI Sunset Execution — Remove the PyQt6 Surface, Fold Legacy Analytics, Retire Scaffold Modules

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #016 (GUI Migration & Qt Sunset, formerly B2), P1
- **Tags:** architecture, web-migration, legacy, sunset, dependencies, process

---

## Context

The strangler migration from the PyQt6 desktop application to the
FastAPI/Jinja2/HTMX web variant (ADR-0033, ADR-0039) completed its final
block in May 2026. Since then the web variant is the primary surface;
the GUI "still ships in the repository as a sunset candidate (roadmap
B2) but receives no new features" (`docs/architecture.md`, §Where the
project is today). Roadmap #016 tracks the sunset at priority P1.

The full-codebase review of 2026-07-02 quantified the carrying cost of
keeping the dormant surface:

1. **Dependency weight.** `PyQt6>=6.6` is an unconditional core
   dependency in `pyproject.toml`, and `pytest-qt` sits in the dev
   extra. Every install — including headless web deployments and CI
   runners — pulls a full Qt toolkit it never uses.
2. **Dual truth.** `gui/widgets/_statistics_helpers.py` duplicates a
   subset of `services/analytics/statistics.py`, held in sync only by
   1e-12 parity tests. The top-level `analytics/` package exists solely
   because the GUI predates `services/analytics/` (ADR-0045 names it a
   strangler artefact and already plans the fold "when the GUI
   sunsets").
3. **Context weight.** `gui/` (~492 KB) plus `tests/gui/` inflate every
   Repomix snapshot, consuming Opus/Sonnet context budget in every
   AI-assisted session (ADR-0015 workflow) for code that will never be
   extended.
4. **Audit surface.** A second UI in the same artefact that is *not*
   connected to Postgres, bypasses RLS entirely (in-memory DataStore,
   ADR-0004/ADR-0041), and has no authentication is a standing
   explanation item in any external review (BAIT/VAIT, DORA, client
   operational due diligence). Deleting it is simpler than defending
   it.
5. **Rule complexity.** Three architecture rules exist only to contain
   Qt: the ADR-0011 allowance for `services/ai_service_qt.py`, the
   deprecation shim `services/ai_service.py`, and the `gui/` row in the
   dependency graph. Each is a rule an AI session can misapply.

Two facts established by the review shape the cut:

- **The Telegram bot does not depend on the GUI entry point.**
  `bot.telegram_bot.start_bot` is invoked from both `main.py` (GUI) and
  `web/main.py` (lifespan hook). Deleting `main.py` does not orphan the
  bot.
- **The 1e-12 parity coverage survives the deletion.** The Qt reference
  implementations were previously lifted verbatim into
  `tests/services/analytics/test_statistics.py` (see its docstrings
  "Lifted from gui/widgets/…"). The parity tests pin the analytics
  layer against those in-test references, not against live `gui/`
  imports. The same holds for
  `tests/services/analytics/test_qt_consistency_efficient_frontier.py`,
  which imports only `analytics.portfolio_optimizer` (folded, not
  deleted, by this ADR).

One entanglement **prevents** a single-pass removal of everything
Qt-era: the in-memory DataStore complex. `core/data_store.py` is still
consumed by the DataStore-coupled reporting engine
(`services/reporting/report_engine.py:31`) and by three module shells
(`modules/back_office/saa.py`, `modules/front_office/data_import.py`,
`modules/investor_communication/portfolio_review.py`). Of these,
`data_import.py` hosts `load_excel`, which the **web** upload route
consumes today (`web/routes/data_import.py:108`; relocation is Category-A
item A5 of the 2026-07-02 review), and
`services/reporting/data_providers/_calculations.py` (`compute_irr`) is
consumed by the live web analytics
(`services/analytics/investment_returns.py:32`,
`services/analytics/portfolio_aggregation.py:44`). The DataStore complex
therefore cannot be deleted blindly with the GUI and is explicitly
staged out of scope (see Decision §5).

Separately, the review identified seven module files that are pure
scaffolds — registered `BaseModule` subclasses whose public methods are
`...` bodies with full docstring specifications, dating from the module
lifecycle's Scaffold stage (ADR-0016) and never implemented because the
corresponding capabilities shipped as services + web sections instead.
Their deletion is bundled here because they are Qt-era planning
artefacts and their registry entries mislead both human readers and AI
sessions about what exists.

## Decision

Execute the GUI sunset as **Stage 1** of a two-stage decommission. Stage
1 is this ADR; Stage 2 (the DataStore complex) is deferred to a
follow-up roadmap item and a future ADR.

### 1. Removal inventory (Stage 1)

| # | Artefact | Action | Verified consumers after removal |
|---|---|---|---|
| R1 | `gui/` (entire tree, incl. `gui/theme.py`, `gui/theme_persistence.py`) | Delete | none |
| R2 | `main.py` (GUI entry point) | Delete | none — bot startup also lives in `web/main.py` |
| R3 | `tests/gui/` (entire tree) | Delete | n/a |
| R4 | `tests/assistants/test_shirley_widget.py`, `tests/assistants/test_scraper_widget.py`, `tests/back_office/test_saa_widget.py` | Delete (verified `from gui.…` imports) | n/a |
| R5 | `services/ai_service_qt.py` (Qt adapter, ADR-0011/0038) | Delete | only `gui/` and the R6 shim |
| R6 | `services/ai_service.py` (deprecation shim re-exporting the Qt adapter) | Delete | none in production; migrate any residual test imports to `services/ai_service_core` |
| R7 | `pyproject.toml`: `PyQt6>=6.6` dependency; `pytest-qt` dev dependency; `portfoliflow-gui = "main:main"` script; `"gui*"` in `packages.find` | Remove | n/a |
| R8 | `analytics/` top-level package (`portfolio_optimizer.py`, `sample_window.py`, `__init__.py`) | **Fold** into `services/analytics/` (move, do not rewrite) | imports updated in 6 production files + 5 test files (inventory in Implementation Notes) |
| R9 | Scaffold modules: `modules/front_office/charts.py`, `statistics.py`, `timeseries.py`, `export.py`, `portfolio_optimizer.py`; `modules/back_office/cashflow.py`, `portfolio_tracking.py` | Delete; archive their docstring specifications under `docs/module_specs/` (one file per module, verbatim class/method docstrings) | registry entries and area `__init__.py` imports removed (the symmetric inverse of the ADR-0016 three-line rule) |
| R10 | Dead methods riding the R8 fold: `PortfolioOptimizer.optimize_for_target_return` / `optimize_for_target_risk` (`analytics/portfolio_optimizer.py:531/:563`, zero references repo-wide) | Delete during the move | none |

### 2. Explicitly retained (Stage 1)

- `core/theme_service.py`, `core/ui_theme.py`, `core/chart_theme.py`,
  `scripts/generate_theme_artifacts.py` — the web theme pipeline
  (ADR-0021/0025/0032) is independent of Qt. Only `gui/theme.py` (the Qt
  *applier*) is deleted with R1.
- `matplotlib` and `squarify` dependencies — still consumed by
  `services/reporting/chart_builders/` (the DataStore-era report
  renderer, Stage 2) and the Report Scraper path. Re-evaluated in Stage 2.
- All Qt-freedom regression guards
  (`tests/regression/test_ai_service_core_qt_free.py`,
  `tests/bot/test_telegram_bot.py::test_no_qt_import`,
  `tests/regression/test_analytics_layer_pure.py`, voice-layering
  guard). They become trivially green but continue to document and
  enforce the invariant at negligible cost — and they would catch any
  accidental Qt reintroduction via a transitive dependency.
- The parity/consistency tests under `tests/services/analytics/` — their
  reference implementations are self-contained (see Context).
- Module shells with live consumers or DataStore entanglement:
  `modules/back_office/saa.py`, `benchmarks_attribution.py`,
  `limits.py`, `modules/front_office/data_import.py`, `overview.py`,
  `modules/investor_communication/portfolio_review.py`, all of
  `modules/assistants/` and `modules/decision_console/`, `modules/admin/`.

### 3. Documentation and glossary consequences

- **Glossary v3:** the `Widget` and `Panel` rows (legacy-Qt terms,
  ADR-0084 §legacy) are removed from `docs/architecture.md` and were
  already absent from CLAUDE.md's abbreviated table. The §Legacy
  sections describing the Qt path are removed or rewritten to one
  historical paragraph pointing at this ADR.
- **Dependency graph:** the `gui/` row and the `services/ai_service_qt.py`
  allowance are removed from both `docs/architecture.md` and
  `CLAUDE.md`; the "only file permitted to import PyQt6" rule is
  replaced by an unconditional "no PyQt6 anywhere" rule.
- **ADR statuses:** ADR-0011 → *Superseded by ADR-0094*. ADR-0038's Qt
  adapter half is historically fulfilled; its core (Qt-free
  `AIServiceCore`) remains the live decision — annotate its revision
  history rather than superseding. ADR-0004 (in-memory DataStore) and
  ADR-0041 (strangler coexistence) remain *Accepted* until Stage 2.
- **Roadmap:** #016 → `done` upon completion of both sessions. A new
  item "DataStore complex decommission (Stage 2)" is created (next free
  ID per roadmap header) with dependencies: A5 (load_excel relocation)
  and #001 (Bundle-based PDF path confirms the DataStore ReportEngine
  has no future consumer).

### 4. Execution protocol

Per ADR-0014/0015: operator sets a `demo-stable-pre-qt-sunset` git tag
and a checkpoint commit; the change lands as **two Claude Code
sessions** (Session 1 mechanical removal + fold; Session 2
documentation), each a single Conventional Commit, reviewed against the
checkpoint. Full prompts in Implementation Notes.

### 5. Stage 2 (out of scope, recorded for traceability)

Deferred to a follow-up ADR: `core/data_store.py`,
`core/persistent_data_store.py`, the DataStore-coupled
`services/reporting/report_engine.py` + `ProviderContext` path (the
`data_providers/_calculations.py` module survives regardless — it is
web-consumed), `modules/investor_communication/portfolio_review.py`
(orphaned after R1 but DataStore-entangled),
`modules/back_office/saa.py`'s DataStore usage, the
`data_store_entries` table (schema history is immutable; the table can
be dropped by a forward migration when the model goes), and the
matplotlib/squarify dependency question.

## Rationale

The decisive argument is asymmetry of cost. Keeping the GUI costs
something on every install, every snapshot, every AI session, and every
external review — and buys nothing, because the surface receives no
features, shares no data path with production (ADR-0041), and has no
users once the operator works web-first. Deleting it is a large but
almost entirely *red* diff, which is the cheapest kind to review.

Folding `analytics/` now rather than later follows ADR-0045's own plan
("will be folded into `services/analytics/` when the GUI sunsets") and
removes a permanent source of import-path ambiguity for AI sessions
(two packages named `analytics`).

Retiring the scaffolds honours the original intent of the module
lifecycle without keeping its dead ends: the specifications were the
valuable output of the Scaffold stage, so they are archived as
documents; the empty registry entries were the liability, so they go.
The web sections that share names with the scaffolds
(`cashflow`, `portfolio-tracking` "planned" placeholders,
`export`/`timeseries` catalogue entries) are template- and
catalogue-level constructs, verified independent of the module classes —
their fate is a product decision handled by review item A1 and the
roadmap, not by this ADR.

Staging the DataStore complex out is the risk-containment decision. The
GUI removal is provably consumer-free; the DataStore removal is not yet
(live `load_excel`, live `compute_irr`, pending #001 render-path
confirmation). Bundling them would turn a red diff into a refactor.

## Alternatives Considered

**A. Keep the GUI dormant until multi-user (#015) lands.** Rejected:
none of the carrying costs decrease by waiting, and #015 makes the
unauthenticated, RLS-bypassing second surface *more* anomalous, not
less.

**B. Big-bang removal including the DataStore complex.** Rejected: the
DataStore path has two live web-side consumers (`load_excel`,
`compute_irr`) and one pending design confirmation (#001 Path B). The
entanglement is exactly the kind of hidden coupling the strangler
pattern exists to avoid; Stage 2 after A5 is strictly safer.

**C. Extract `gui/` into a separate archival repository.** Rejected:
git history already preserves every state (`demo-stable-*` tags exist
for co-deployment points); a second repo adds custody burden with no
consumer.

**D. Delete the scaffolds' specifications along with the files.**
Rejected: the docstring specs for `cashflow` (J-curve, Yale-model
forecast, liquidity requirements) and `export` (PDF) describe roadmap
items #023 and #001 and retain planning value. Archival costs one
directory.

## Consequences

Positive: PyQt6 and pytest-qt leave the dependency tree (headless
installs shrink by the full Qt toolkit); the Repomix snapshot loses
`gui/`, `tests/gui/`, and the widget tests (measurably more context
budget per ADR-0015 session); one import rule, one adapter, one shim,
and one glossary legacy section disappear; the statistics dual-truth
ends; the audit narrative simplifies to "one surface, one auth path,
RLS everywhere".

Negative / accepted: the desktop variant is gone — any future offline
desire is served by the web stack on localhost, not by resurrecting Qt.
Git history and the `demo-stable-*` tags remain the archival record.
Contributors with muscle memory for `portfoliflow-gui` get an
uninstalled script (release-note item).

Neutral: ~37 dead symbols identified in the review shrink to a handful
(most lived in `gui/` or the scaffolds); the remainder is Category-A
item A4.

## Implementation Notes

### Operator pre-steps

1. `git tag demo-stable-pre-qt-sunset` on a green working tree.
2. Checkpoint commit per ADR-0014.
3. Run the full test suite once to record the green baseline
   (count of passed tests noted for post-comparison).

### Session 1 prompt — mechanical removal and fold

```
GUI Sunset Stage 1 — mechanical removal (ADR-0094). Read ADR-0094 first;
it is the authoritative scope. Do exactly this, nothing more:

1. DELETE: the entire gui/ tree; main.py; the entire tests/gui/ tree;
   tests/assistants/test_shirley_widget.py;
   tests/assistants/test_scraper_widget.py;
   tests/back_office/test_saa_widget.py;
   services/ai_service_qt.py; services/ai_service.py.

2. FOLD analytics/ into services/analytics/: git-move
   analytics/portfolio_optimizer.py and analytics/sample_window.py into
   services/analytics/ VERBATIM (no reformatting, no logic changes),
   delete analytics/__init__.py and the empty package. While moving
   portfolio_optimizer.py, delete the two dead methods
   optimize_for_target_return and optimize_for_target_risk (ADR-0094 R10).
   Update imports from `analytics.` to `services.analytics.` in:
   modules/back_office/saa.py, services/chart_specs/efficient_frontier.py,
   services/portfolio_analysis/portfolio_analysis_service.py,
   services/results_serialization.py, services/saa/saa_service.py,
   services/analytics/efficient_frontier.py, and in the test files:
   tests/services/test_chart_specs_efficient_frontier.py,
   tests/services/test_portfolio_analysis_service.py,
   tests/services/test_results_serialization.py,
   tests/services/analytics/test_efficient_frontier.py,
   tests/services/analytics/test_qt_consistency_efficient_frontier.py.
   Then grep the whole repo for any remaining `from analytics` /
   `import analytics` and fix stragglers the inventory missed.

3. RETIRE SCAFFOLDS: delete modules/front_office/charts.py,
   statistics.py, timeseries.py, export.py, portfolio_optimizer.py and
   modules/back_office/cashflow.py, portfolio_tracking.py. Before
   deleting, copy each file's module- and method-level docstrings
   verbatim into docs/module_specs/<module_name>.md (create the
   directory; one file per module; header line naming the source file
   and ADR-0094). Remove each module's import line from its area
   __init__.py and nothing else in those files. Delete any test file
   whose sole subject is a deleted scaffold module.

4. PYPROJECT: remove the PyQt6 dependency, the pytest-qt dev
   dependency, the portfoliflow-gui script entry, and "gui*" from
   packages.find. Touch nothing else in pyproject.toml (matplotlib and
   squarify STAY — Stage 2 concern).

5. DO NOT touch: core/data_store.py, core/persistent_data_store.py,
   services/reporting/ (any file), core/theme_service.py,
   core/ui_theme.py, modules/back_office/saa.py beyond the import fix,
   modules/front_office/data_import.py, modules/front_office/overview.py,
   modules/investor_communication/portfolio_review.py, any regression
   guard test, any Alembic migration, any template or CSS.

Acceptance criteria (verify and report each):
- `grep -rn "PyQt6" --include='*.py' .` matches only regression-guard
  assertions and comments, no import statements.
- `grep -rn "from gui\|import gui\b" --include='*.py' .` is empty.
- `grep -rn "^from analytics\|^import analytics" --include='*.py' .` is
  empty.
- `pip install -e .` in a clean venv pulls no PyQt6.
- Full pytest run green; report the passed count vs. the operator's
  recorded baseline (expected delta: exactly the deleted test files).
- The module registry still imports cleanly:
  `python -c "import modules; from modules.module_registry import registry; print(sorted(registry.list_all()))"`.

Conventional Commit: `refactor(sunset)!: remove PyQt6 surface, fold
legacy analytics, retire scaffold modules (ADR-0094 Stage 1)`.
The `!` marks the removed portfoliflow-gui entry point.
```

### Session 2 prompt — documentation

```
GUI Sunset Stage 1 — documentation pass (ADR-0094, follows the merged
Stage-1 code commit). Documentation files only; zero code changes.

1. docs/architecture.md: remove the gui/ row from the dependency graph
   and the layered-architecture rules; remove the ai_service_qt
   allowance and the ai_service.py shim mentions; delete the "### gui/
   (sunset)" layer section and the Widget/Panel glossary rows; rewrite
   the §Legacy references into one short historical paragraph citing
   ADR-0094 and the demo-stable-pre-qt-sunset tag; update the AI-service
   adapter list from three adapters to two (web SSE, Telegram); state
   that analytics/ was folded into services/analytics/ (ADR-0045
   fulfilled) and update the top-level layer diagram accordingly.
2. CLAUDE.md: same graph and rule edits (remove gui/ row, remove the
   PyQt6 allowance — the rule becomes "no PyQt6 imports anywhere, no
   exceptions"); remove Widget/Panel legacy notes; update the layer
   listing.
3. docs/adr/0011-…: status → "Superseded by ADR-0094" with a revision-
   history row. docs/adr/0038-…: add a revision-history row noting the
   Qt adapter was removed by ADR-0094 while the Qt-free core remains
   the live decision. Do NOT change ADR-0004 or ADR-0041.
4. docs/adr/README.md index: status updates for 0011; add 0094 if the
   index lists it as Proposed and the operator has accepted it —
   otherwise leave status lines for the operator and only fix
   cross-references.
5. docs/roadmap.md: #016 → done (date, ADR-0094) and move to Shipped;
   create the Stage-2 follow-up item "DataStore complex decommission"
   using the next free ID from the roadmap header (then bump the
   header), category Loose ends, priority P2, dependencies: review item
   A5 and #001; description per ADR-0094 §Decision 5.
6. readme.md: remove GUI launch instructions / portfoliflow-gui
   mentions if present.

Acceptance criteria:
- `grep -rn "portfoliflow-gui\|ai_service_qt\|pytest-qt" docs/ CLAUDE.md readme.md`
  matches only ADR historical records (0011/0038/0094) and the audit
  ledgers.
- `grep -n "gui/" CLAUDE.md docs/architecture.md` matches only the
  historical paragraph and ADR references.
- No .py file modified.

Conventional Commit: `docs(sunset): reconcile steering documents after
Qt removal (ADR-0094 Stage 1)`.
```

### Post-completion operator actions

- Flip this ADR to Accepted; record both commit hashes in the Revision
  History.
- Regenerate the Repomix snapshot and note the size delta in the
  roadmap Shipped entry (expected: several hundred KB).
- If CI (review item B1) is live by then, both sessions must land
  through it.

## Compliance & Audit Relevance

Removing the unauthenticated, RLS-bypassing desktop surface reduces the
system to a single access path governed by session auth (ADR-0036),
tenant RLS (ADR-0035), and the audit substrate (ADR-0019). For external
reviews this converts a standing explanation item ("why does a second
UI exist that bypasses tenant isolation?") into a one-line historical
note with a decision record. The `demo-stable-pre-qt-sunset` tag
preserves full reproducibility of the pre-removal state, consistent
with the immutable-decision-log principle.

## References

- Roadmap #016 (GUI Migration & Qt Sunset, formerly B2)
- ADR-0011 (superseded by this ADR), ADR-0038, ADR-0039, ADR-0041,
  ADR-0045 (fold plan), ADR-0004 (Stage 2), ADR-0084 (glossary v2)
- Full-codebase review of 2026-07-02 (dead-code inventory, consumer
  maps, parity-test verification)
- Category-A handover of 2026-07-02, items A1 (section catalogue) and
  A5 (`load_excel` relocation — Stage-2 dependency)

## Revision History

| Date | Change |
|---|---|
| 2026-07-02 | Initial draft (Proposed). |
| 2026-07-11 | Accepted against the shipped code. **Stage 1 executed 2026-07-02** (roadmap #016 shipped): the PyQt6 surface under `gui/` is removed and PyQt6 is no longer importable anywhere. **§5 Stage 2 (the DataStore complex) remains open** — `core.data_store.DataStore` survives under the Strangler coexistence (ADR-0041), consumed only by the DataStore-coupled reporting engine and a few module shells. Stage 2 is tracked as roadmap **#035** and is not covered by this acceptance. |
