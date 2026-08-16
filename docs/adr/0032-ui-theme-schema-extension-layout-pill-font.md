# ADR-0032: UI Theme Schema Extension for Layout, Pill, and Font Tokens

- **Status:** Proposed
- **Date:** 2026-04-29
- **Deciders:** PortfoliFLOW project owner
- **Tags:** ui, architecture, process

---

## Context

ADR-0025 introduced the UI-theming system, externalising colour and
base-font parameters from `gui/theme.py` constants into
`config/ui_theme*.json`. The pattern — *style is data, not code* —
mirrored ADR-0021's earlier externalisation of chart visual parameters
and shipped three variants (Cyberpunk Dark, Light, Corporate Blue) on
top of a single loader (`core/ui_theme.py`) and a single stylesheet
builder (`gui/theme.py`).

The QSS-polish pass on 2026-04-29 (recorded in the operator's
`handover_ui_polish.md`) concluded that the next visible jump in
perceived polish is not in QSS-level surface treatment but in:

- **Spacing and density** between widgets in a layout (currently set
  with hardcoded magic numbers in widget code such as
  `gui/widgets/collapsible_section.py::_HEADER_HEIGHT = 32`,
  `_ANIMATION_DURATION_MS = 200`, `_PILL_PADDING_PX = 6`).
- **Visual hierarchy** between heading, label, and body text (currently
  uniform because `gui/theme.py` has no slots beyond a single base font
  size).
- **Pill styling** for the per-investment headline pills used by
  `CollapsibleSection` and similar atoms (currently a mix of
  hardcoded padding/radius and theme-driven colours).
- **Tabular figures** in numeric tables — a visible institutional
  polish that QSS cannot express. The QSS-polish pass briefly tried
  `font-feature-settings: "tnum";` and discovered that this CSS3
  property is not part of Qt's QSS dialect; Qt logged an `Unknown
  property` warning per styled widget on every theme application.
  The fix was to remove the line. Tabular-figures support belongs in
  the QFont API, not in QSS.

The decision PortfoliFLOW must make is *whether to extend ADR-0025's
externalisation pattern to layout, pill, and an expanded font section,
or to leave those tokens as scattered magic numbers in widget code*.
This decision is `Proposed` because no code yet implements the
extension; the schema in the JSON files, the loader validation in
`core/ui_theme.py`, the consumer wiring in `gui/theme.py`, and the
widget-code migration are all future work tracked here.

This decision touches Maintainability (a single externalisation
discipline replaces scattered magic numbers across 15–25 files) and
Usability (themable layout density supports user accessibility
preferences once the deferred density ADR follows).

## Decision

PortfoliFLOW will extend `config/ui_theme*.json` with three new
top-level sections — `layout`, `pill`, and an expanded `font` — under
the same loader, the same `ThemeService`-resolved active filename, and
the same lockstep update obligation across all shipped variants. The
externalisation pattern is identical to ADR-0025's; the new sections
apply it to a different class of tokens.

**`layout` section.** Layout primitives that today live as magic
numbers in widget code: corner radii (`radius_sm`, `radius_md`,
`radius_lg`), the spacing unit, scrollbar width, and border width.

**`pill` section.** A first-class home for pill-style atoms used by
`CollapsibleSection` and any future component that needs a distinct
"summary chip" visual: background, foreground, border, radius,
horizontal padding, vertical padding.

**Expanded `font` section.** Adds heading and label sizes, weight
tokens (`weight_normal`, `weight_bold`), and a boolean
`tabular_figures`. The base-font slots from ADR-0025 (`family`,
`size_base`) remain unchanged.

**`tabular_figures` is applied via the QFont API, not via QSS.** The
configuration belongs in the JSON theme (so it can be turned off in a
print theme, on in a dark theme, and so on), but the *application*
crosses the data/code seam at the loader, not at the schema. A
planned helper, `gui/theme.py::apply_application_font(app)`, will
read `font.tabular_figures` from the active theme and apply it via
`QFont.setFeature("tnum", 1)` (Qt 6.7+; older Qt falls back
gracefully without tabular figures). The helper is called once from
`main.py` after `QApplication` construction, before `MainWindow`.

**Density is deferred to a separate, later ADR.** A `density` token
(`compact` / `comfortable` / `spacious`) was discussed in the
UI-polish thread and would multiply into spacing. It is intentionally
out of scope here for sequencing reasons: it triples the test surface
for layout tokens, and the underlying tokens this ADR introduces are
the prerequisite for it. The deferral mirrors ADR-0025's own
ship-without-density precedent.

**Lockstep update obligation.** Every shipped variant
(`config/ui_theme.json`, `config/ui_theme_light.json`,
`config/ui_theme_corporate_blue.json`) must populate the new sections
when the schema lands. A variant missing a section is a
`ConfigurationError` at load time, the same loud-failure shape
established in ADR-0025.

This ADR records the schema-extension *decision*. It does not write
the implementation prompt. The widget-code migration from hardcoded
magic numbers to layout tokens is a multi-session follow-up tracked
in the handover; it is named here as planned work but is not a
precondition for this ADR's acceptance — the schema can land first,
and consumers can migrate incrementally afterwards.

## Rationale

- **Externalisation discipline already exists at this layer; this
  decision extends it.** ADR-0025 established that UI styling
  parameters are JSON-driven, theme-switchable, and discovered via
  `ThemeService`. Layout, pill, and extended-font tokens are the
  same shape of parameter at the next level of granularity. Keeping
  them in code re-creates the very anti-pattern ADR-0025 cleared up
  for colour and base-font tokens.
- **Themable density is a real institutional preference.** Users
  comparing PortfoliFLOW with Bloomberg-adjacent tools expect a
  "compact mode". That preference lives orthogonally to the colour
  theme. Once layout tokens exist, density-as-multiplier is a small
  addition to a separate ADR; without layout tokens, it has no
  ground to sit on.
- **`tabular_figures` belongs in `font`, not in `layout`, and is
  applied via QFont, not via QSS.** The classification follows
  what the token *is* (a typography feature) rather than what it
  *affects visually* (numeric column alignment). The application
  seam between data and code is at the loader: the JSON declares
  the preference, `gui/theme.py::apply_application_font` realises
  it. Hiding `tabular_figures` in widget code per-table would
  re-create the scattered-magic-number anti-pattern; using QSS
  was tried in the QSS-polish pass and produced 250+ Qt warnings.
- **Pill as a first-class section reflects what a pill is.** A
  pill has its own padding and radius that are not colour tokens
  and are not generic layout primitives — they are the visual
  contract of a distinct atom. Collapsing pill styling into
  `colors` would re-create the very ambiguity ADR-0025 cleared up
  by separating concerns into `background`, `accent`, `text`,
  `border`, and `semantic`.
- **Sequencing without `density` keeps the first extension small.**
  Including `density` would triple the test surface (every consumer
  has to render correctly at three densities) and would entangle
  the schema-extension decision with a UX decision about default
  density. Splitting them keeps each ADR focused on one decision.
- **The lockstep cost is real and worth naming.** Three theme
  variants ship today; every new section means three coordinated
  edits. This is a Negative Consequence and is recorded as such.
  It is also the price of the consistency benefit ADR-0025
  established and this ADR continues.

## Alternatives Considered

- **Keep layout values hardcoded in widget code.** Rejected — the
  same argument that drove ADR-0025 (styling is data, not code;
  institutional users want themable layout density without
  recompiling) applies one level deeper. Hardcoded magic numbers
  cannot be theme-switched.
- **Express `tabular_figures` in QSS via `font-feature-settings`.**
  Rejected — `font-feature-settings` is CSS3 and not part of Qt's
  QSS dialect. The QSS-polish pass demonstrated this empirically
  by producing one `Unknown property font-feature-settings`
  warning per styled widget on every theme application. The QFont
  API is the right seam.
- **Include `density` in this first schema extension.** Rejected
  for sequencing — see *Rationale* above. Recorded as a follow-up
  ADR candidate.
- **Add layout tokens but skip the pill section, pushing pill
  styling into `colors`.** Rejected — pills are a distinct visual
  atom with their own padding and radius that are not colour
  tokens; collapsing them into `colors` would re-create the very
  ambiguity ADR-0025 cleared up for the colour layer.
- **Keep magic numbers in widget code now and revisit when the
  application has a mature theme switcher in the GUI.** Rejected
  — the theme switcher already exists (ADR-0025); waiting longer
  just accumulates more locations to migrate later.

## Consequences

### Positive

- The four-tier externalisation (colour + base-font in ADR-0025,
  plus layout + pill + extended-font in this ADR) gives
  `gui/theme.py` and the consuming widget code a uniform,
  single-source-of-truth pattern.
- Density-as-multiplier (planned for the later ADR) becomes a
  cheap addition once the underlying tokens exist.
- Tabular figures in tables — a small but visible institutional
  polish — become themable rather than hidden in widget code.
- Visual hierarchy across heading / label / body text becomes
  configurable per variant rather than uniformly imposed by a
  single base-font size.
- The pattern stays consistent across `gui/theme.py`'s
  re-exported constants: new layout / pill / font tokens follow
  the existing `BG_PRIMARY`, `ACCENT_RED`, `SEMANTIC_*` shape
  rather than introducing a new convention.

### Negative

- All three shipped JSON variants
  (`config/ui_theme.json`, `config/ui_theme_light.json`,
  `config/ui_theme_corporate_blue.json`) must be kept in lockstep
  with new sections. A variant missing a section will be a
  `ConfigurationError` at load time.
- Schema validation in `core/ui_theme.py` will need extension to
  cover the new sections. The loud-failure shape from ADR-0025
  carries forward.
- Widget-code migration from hardcoded magic numbers is a
  multi-session follow-up. Realistic scope: 15–25 files. Review
  surface is non-trivial.
- Once `apply_application_font` is wired, the QFont feature path
  depends on Qt 6.7+ for `setFeature("tnum", 1)`. The application
  still works on older Qt — it simply does not get tabular
  figures. This compatibility caveat must be noted in the
  implementation prompt.

### Neutral / Follow-ups

- **Density ADR.** A follow-up ADR introducing
  `density.preset = "compact" | "comfortable" | "spacious"` as a
  multiplier into `layout.spacing_unit` and related slots, written
  once the layout tokens have been migrated and have proven
  themselves under the existing variants.
- **QFont application hook.** The planned
  `gui/theme.py::apply_application_font(app)` is a single seam
  for typography features that QSS cannot express. Future
  font-feature additions (e.g. small caps, ligature toggles)
  follow the same pattern.
- **Widget migration sessions.** The handover sketches a
  four-session breakdown (visual atoms, Front Office widgets,
  Back Office widgets, Settings / Admin / panels). The breakdown
  lives in the handover, not in this ADR.
- **Chart-cohesion review.** Out of scope here; flagged in
  handover §3 Priority 4 as future work to verify that
  matplotlib output integrates with the polished UI.

## Implementation Notes

This ADR is `Proposed`. All affected paths below are **planned**
and do not yet exist in the form described.

- Affected files (planned): `config/ui_theme.json` *(planned
  schema extension)*, `config/ui_theme_light.json` *(planned
  schema extension)*, `config/ui_theme_corporate_blue.json`
  *(planned schema extension)*, `core/ui_theme.py` *(planned
  validation extension)*, `gui/theme.py` *(planned consumption
  of new tokens, planned `apply_application_font` helper)*.
- Application seam (planned):
  `gui/theme.py::apply_application_font(app)` — reads
  `font.tabular_figures` from the active theme, applies via
  `QFont.setFeature("tnum", 1)` on Qt 6.7+. Called once from
  `main.py` after `QApplication` construction, before
  `MainWindow`. Marked *(planned)*.
- Migration of existing magic numbers (planned, not part of
  the schema-extension decision itself): hardcoded constants in
  `gui/widgets/collapsible_section.py` and similar sites
  migrate to the new layout / pill tokens incrementally over a
  multi-session follow-up. Marked *(planned)*.
- Schema-version bump (planned): the existing `_version` field
  in each `config/ui_theme*.json` file should be bumped when
  the new sections land, mirroring ADR-0021's versioning
  precedent.
- Existing files this ADR builds on (verified to exist today):
  `config/ui_theme.json`, `config/ui_theme_light.json`,
  `config/ui_theme_corporate_blue.json`, `core/ui_theme.py`,
  `gui/theme.py`. None of these is modified by this ADR; they
  are the consumers / loaders the planned schema extension
  will pass through.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (a single externalisation discipline replaces scattered magic
  numbers across many files; theme variants are easier to keep
  consistent), Usability (themable layout density supports user
  accessibility preferences when the deferred density ADR
  follows).
- **Regulatory references:** Low. Like ADR-0025, this is
  primarily a UX / Maintainability decision; it does not by
  itself touch BAIT / VAIT, DORA, or SOC 2.
- **Audit evidence (planned, not yet present):** schema version
  field bumped in each `config/ui_theme*.json` *(planned)*; new
  sections present in each variant *(planned)*; the
  `gui/theme.py` consumer reading layout tokens via the same
  `_t = get_ui_theme()` pattern as ADR-0025 *(planned)*; the
  `apply_application_font` helper called once from `main.py`
  *(planned)*. All planned and not yet present in the working
  tree.

## References

- ADR-0021 (Chart Theming Externalised to JSON — secondary
  precedent for the *style is data, not code* framing)
- ADR-0025 (UI Theming System with Multi-Variant Support — the
  primary precedent; this ADR extends ADR-0025 without
  superseding it, and ADR-0025 remains `Accepted` and unchanged)

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-04-29 | PortfoliFLOW project owner   | Initial draft. Proposed extension of `config/ui_theme*.json` with `layout`, `pill`, and an expanded `font` section, deferring `density` to a follow-up ADR. No code yet implements the schema extension. |
| 2026-05-04 | PortfoliFLOW project owner   | Status remains **Proposed**. Sub-Strang 2c shipped the design-token pipeline ADR-0037 §7 calls for: `scripts/generate_theme_artifacts.py` reads `config/ui_theme*.json` plus `config/chart_theme.json` and emits `web/static/css/theme.css`. The pipeline already projects the *existing* schema (background / accent / button / text / border / font / semantic) onto CSS custom properties on `:root` plus per-variant scopes, with a pre-commit hook (`.pre-commit-config.yaml`) and an idempotency-checked smoke test. The schema extension this ADR specifies — new `layout`, `pill`, and expanded `font` sections, plus the QFont-API path for `tabular_figures` — remains **unimplemented**: the new keys are not yet in the JSON files, `core/ui_theme.py` still has no validation hooks for them, `gui/theme.py` still inlines the layout magic numbers as literals, and `apply_application_font` does not exist. The token pipeline is therefore *partial* — sufficient for the web variant's MVP styling, but not a substitute for the schema extension itself. ADR stays Proposed; the extension will land together with the widget-code migration in a follow-up sub-strang or phase. Decider: PortfoliFLOW project owner. |
