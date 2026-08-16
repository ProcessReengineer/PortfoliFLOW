# ADR-0025: UI Theming System with Multi-Variant Support

- **Status:** Accepted
- **Date:** 2026-04-27
- **Deciders:** PortfoliFLOW project owner
- **Tags:** ui, process, architecture

---

## Context

PortfoliFLOW renders a PyQt6 desktop GUI whose visual parameters — background
colours, accent colours, font family and base size, button states, semantic
status colours (success / warning / info / error / muted, plus table-cell
backgrounds and confidence indicators) — must be consistent across every
panel, widget, and dialog. Until this ADR these values lived as module-level
constants in `gui/theme.py` and were then re-imported by widgets.

ADR-0021 externalised **chart-level** visual parameters into
`config/chart_theme.json`. The same justification — single source of truth,
brand changes as a one-file edit, no hardcoded hex literals scattered across
widget code — applies one layer up to the **UI itself**: the QSS stylesheet,
button colours, sidebar text colour, semantic indicators. Without an
analogous externalisation, brand changes would still be a multi-file
search-and-replace exercise outside the chart layer, and Phase B's
user-selectable themes would have nowhere to live.

A second concern is that the user can plausibly want more than one shipped
look (current default is a "Cyberpunk Dark" variant; a corporate-blue and a
light variant are also wanted) and to switch between them without rebuilding
the application. That requires (a) discovering shipped theme files at
startup, (b) telling the loaders which file is currently active, and
(c) persisting the user's choice across sessions.

This decision is primarily a Maintainability / Usability concern. It is not
itself security- or audit-relevant, but it consolidates yet another class
of hardcoded values into a reviewable config file, which is mildly positive
from an audit perspective.

## Decision

PortfoliFLOW introduces a UI-level theming system that sits alongside, not
in place of, the chart theming system of ADR-0021. The two systems share
the same shape but operate on different visual concerns.

**Two parallel JSON families.** UI-level visual parameters are stored in
`config/ui_theme*.json`; chart-level parameters remain in
`config/chart_theme*.json` (ADR-0021 unchanged). Each family is an opaque
nested dict with a `_version` field, a free-text `_comment`, and a
human-readable `_display_name` field consumed by the picker UI.

**Multi-variant.** PortfoliFLOW ships three UI theme files:

- `config/ui_theme.json` — the default, "Cyberpunk Dark".
- `config/ui_theme_light.json` — a light variant.
- `config/ui_theme_corporate_blue.json` — a corporate-blue variant.

Exactly one variant is active at runtime; switching is supported per ADR
(see *ThemeService* below).

**Loader: `core/ui_theme.py`.** A small loader analogous to
`core/chart_theme.py`. Exposes `get_ui_theme()` (cached) and
`reload_ui_theme()` (force-reload). The cached dict is the source of truth
for every UI consumer. A missing or malformed file raises
`ConfigurationError` at first access — the application crashes loudly
rather than rendering a broken GUI.

**Service: `core/theme_service.py`.** A framework-agnostic singleton
(`get_theme_service()`) that:

- Discovers shipped theme files in `config/` for both UI and chart
  families using glob patterns (`ui_theme.json`, `ui_theme_*.json`,
  `chart_theme.json`, `chart_theme_*.json`), placing the default file
  first and the rest in alphabetical order.
- Reads each file's `_display_name` field (with a humanised filename
  fallback) and exposes a list of frozen `ThemeInfo(filename,
  display_name, is_default)` records via `list_ui_themes()` and
  `list_chart_themes()`.
- Tracks the **currently active filename** for each kind, validated against
  the discovered list. Setters
  (`set_active_ui_theme_filename`, `set_active_chart_theme_filename`)
  raise `ConfigurationError` on unknown filenames.
- Imports nothing from `gui/`, `services/`, `modules/`, `analytics/`, or
  PyQt6 — see CLAUDE.md dependency rules. The service is intentionally
  framework-agnostic so it can be used from a future headless renderer or
  CLI tool without dragging in Qt.

The service explicitly covers **both** UI and chart themes; it is the
single resolver for "which theme file is active right now", regardless of
which kind. The chart loader (`core/chart_theme.py`) reads its active
filename from the same service so the two families stay in lockstep with
respect to the picker UI.

**Stylesheet builder: `gui/theme.py`.** The application-level QSS builder
imports `get_ui_theme()`, re-exports the loaded values as module-level
constants (`BG_PRIMARY`, `ACCENT_RED`, `SEMANTIC_SUCCESS`, …) so existing
widget code keeps a stable import surface, and provides `build_stylesheet()`
which assembles the full QSS string handed to
`QApplication.setStyleSheet()`.

**Persistence: `gui/theme_persistence.py`.** A QSettings-backed module
that owns the round-trip for the user's last-selected theme. Public API:

- `load_persisted_theme_choices()` — called once at startup, before
  `MainWindow` is constructed. Reads keys `theme/ui` and `theme/chart`
  from the shared `QSettings("PortfoliFLOW", "PortfoliFLOW")` store
  (the same store used by `services/ai_service.py`). On a missing or
  invalid entry, logs a warning and falls back to the default — startup
  must never fail because of stale persistence. After applying choices,
  it explicitly invalidates the UI and chart theme caches so that the
  loaders pick up the user-selected file on first read.
- `save_ui_theme_choice(filename)` and `save_chart_theme_choice(filename)`
  — called from the settings widget when the user picks a theme. They
  validate the filename via the service, persist it, and rely on the
  caller (settings widget) to inform the user that the change takes
  effect on next start (Phase B has no hot-reload).

`gui/theme_persistence.py` imports `PyQt6.QtCore.QSettings` directly. This
is a documented exception parallel to ADR-0011's QSettings exception for
`services/ai_service.py`: the persistence module lives under `gui/`, where
PyQt6 imports are normal, but it is a non-widget module and the QSettings
key prefix `theme/` is shared with the existing AIService keys so the
project keeps a single user-settings store.

This ADR is `Accepted` because the system described above is implemented
and shipped.

## Rationale

- **Mirrors ADR-0021 at a different layer.** The chart-theme decision
  established the "style is data, not code" pattern. Applying it to the
  UI layer is the obvious follow-through; doing so consistently keeps
  the two layers symmetric (loader + JSON + theme service entry).
- **A separate `ThemeService` is the right place for active-filename
  state.** Putting it in `core/ui_theme.py` would force the chart loader
  to import from the UI loader; putting it in `gui/theme.py` would force
  the chart loader to import from the GUI layer (forbidden by
  ADR-0001). A neutral service in `core/` is the layering-clean
  solution.
- **Discovery via glob is a one-time scan.** Theme files are added by
  the developer as JSON files; the service finds them automatically on
  next start. This keeps "ship a new variant" to a one-file change and
  no code edits.
- **`_display_name` lives in the file.** A user-visible label belongs
  next to the data it describes; deriving it from the filename loses
  capitalisation and language nuance.
- **Loud failure on malformed JSON, soft failure on stale QSettings.**
  A broken file is a developer bug that should crash startup so it gets
  fixed; a deleted theme that QSettings still references is a normal
  end-user state and must fall back silently.

## Alternatives Considered

- **Keep a single theme file.** Implicitly rejected — not formally
  evaluated. The user explicitly wanted multiple shipped variants
  (dark / light / corporate-blue), so a single-file design was never
  the destination.
- **Hardcoded stylesheets in widgets.** Implicitly rejected — not
  formally evaluated. This is the pre-ADR-0021 anti-pattern; brand
  changes become multi-file edits and consistency drifts over time.
- **Embed UI theme inside `chart_theme.json`.** Implicitly rejected —
  not formally evaluated. UI and chart concerns evolve independently
  (a new accent colour does not necessarily change chart palette
  ordering, and vice versa). Conflating them would force unrelated
  edits to the same file and would require a single schema for two
  unrelated value sets.
- **Use a third-party Qt theming library (e.g. QDarkStyle, qt-material).**
  Implicitly rejected — not formally evaluated. PortfoliFLOW's brand
  expectations are specific (semantic confidence indicators,
  positive/negative table-cell backgrounds) and unlikely to be served
  by a generic theme library without pervasive overrides; a thin,
  project-specific JSON is a smaller surface than a library
  dependency.

## Consequences

### Positive

- Brand changes are a single-file edit per concern (UI / chart).
- Adding a UI variant is one new JSON file plus a `_display_name`
  field; no code change.
- The picker UI consumes one canonical service (`get_theme_service()`)
  that already lists discovered variants for both kinds.
- Stale QSettings entries cannot prevent startup; loud failures only
  on developer bugs.

### Negative

- Two parallel JSON families (UI + chart) double the schema surface;
  the developer must remember which family owns a given parameter.
- Adding a new visual parameter still requires editing both the JSON
  schema and the consumer code (same drawback as ADR-0021).
- Theme switching is not hot-reloadable in this iteration: changing a
  theme requires an application restart. The settings widget is
  responsible for telling the user.

### Neutral / Follow-ups

- Hot-reload (re-applying the QSS at runtime and re-rendering open
  charts) is a candidate follow-up; out of scope here.
- Per-client theme selection — a future cousin of this decision —
  belongs to the long-term Reporting Engine Style Layer (ADR-0020) and
  may eventually share schemas with the UI theme.
- Schema validation against a JSON Schema (catch malformed entries
  on developer commit, not at first run) is a follow-up similar to
  ADR-0021's note.

## Implementation Notes

- Loaders: `core/ui_theme.py`, `core/chart_theme.py`.
- Service: `core/theme_service.py` (`ThemeService`,
  `get_theme_service()`, `ThemeInfo`).
- Stylesheet builder: `gui/theme.py` (`build_stylesheet()` plus
  re-exported constants).
- Persistence: `gui/theme_persistence.py` (`load_persisted_theme_choices`,
  `save_ui_theme_choice`, `save_chart_theme_choice`).
- Theme files:
  - `config/ui_theme.json` (default — Cyberpunk Dark)
  - `config/ui_theme_light.json`
  - `config/ui_theme_corporate_blue.json`
  - `config/chart_theme.json` (default)
  - `config/chart_theme_light.json`
  - `config/chart_theme_print.json`
- Tests: `tests/core/test_ui_theme.py`,
  `tests/core/test_theme_service.py`,
  `tests/gui/test_theme_persistence.py`,
  `tests/gui/test_theme.py`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (modifiability — visual changes are local; configurability —
  multi-variant), Usability (the user can choose a theme that suits
  their environment).
- **Regulatory references:** Low. This is primarily a UX /
  Maintainability decision; it does not by itself touch BAIT/VAIT,
  DORA, or SOC 2.
- **Audit evidence:** `config/ui_theme*.json` and
  `config/chart_theme*.json` are checked in and versioned; the absence
  of hardcoded colour literals in widget code is checkable by grep, the
  same property ADR-0021 already established for chart code.

## References

- ADR-0021 (Chart theming externalised to JSON — sibling decision; this
  ADR neither supersedes nor extends ADR-0021's scope, only places a
  parallel system at a different layer)
- ADR-0001 (Layered architecture — the service is in `core/` because
  it must be reachable from `gui/`, `services/`, and the chart loader
  without violating the dependency rules)
- ADR-0011 (Acknowledged PyQt6 dependency — the QSettings exception
  there is the precedent for the QSettings import in
  `gui/theme_persistence.py`)
- ADR-0020 (Planned Reporting Engine — the eventual per-client Style
  Layer is a generalisation of the multi-variant idea introduced here)

---

## Revision History

| Date       | Author                       | Change        |
|------------|------------------------------|---------------|
| 2026-04-27 | PortfoliFLOW project owner   | Initial draft. Records the decision behind `core/theme_service.py`, `core/ui_theme.py`, `gui/theme.py`, `gui/theme_persistence.py`, and the `config/ui_theme*.json` family. Code already implemented and in use. |
