# ADR-0037: Frontend Stack — FastAPI + Jinja + HTMX, Server-Side Rendering as Default

- **Status:** Accepted
- **Date:** 2026-05-03
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, frontend, htmx, server-side-rendering

---

## Context

ADR-0033 commits PortfoliFLOW to FastAPI + Jinja templates + HTMX as
the high-level web stack and explicitly rules out an SPA architecture
(React, Vue, Svelte) as the default frontend. What ADR-0033 does not
fix is the layer below that decision: which JavaScript libraries
deliver charts and complex tables, how the streaming surface for
Shirley is shaped on the wire, what the design-token workflow looks
like between PyQt6 stylesheets and web CSS, and where the escalation
points are when HTMX runs out of expressive room.

This ADR fills those concretes. It does not re-argue the strategic
choice against an SPA — that argument lives in ADR-0033 — but it
operationalises the chosen direction enough that Phase 2 work can
start without further architectural debate.

The constraints that shape the answers:

- **Solo-developer ressources.** Every additional build-stack
  component (npm, webpack/Vite, PostCSS, Tailwind, a TS toolchain)
  has a steady-state maintenance cost. The frontend stack must
  survive periods of low frontend activity without rotting.
- **Visual consistency during the strangler period.** The PyQt6
  desktop variant and the new web variant must look like the same
  product while both are live. ADR-0033's Phase 3 makes design-system
  extraction explicit; this ADR commits the workflow.
- **Streaming as a first-class shape.** Shirley's responses stream.
  ADR-0038 fixes the AI-side streaming primitive (an async
  generator); the web frontend has to consume that and render it
  incrementally without inventing a parallel mechanism.
- **Escalation, not exclusion.** A blanket "HTMX only" rule would
  force awkward workarounds where richer interaction is genuinely
  needed. The right shape is HTMX as default plus documented
  escalation paths.

This decision is primarily an engineering choice. It is light on
compliance content because the substantive compliance commitments
(persistence, tenancy, authentication, audit) live in ADR-0034
through ADR-0036; here, ISO 25010 quality attributes (Maintainability,
Portability, Usability) and a WCAG 2.1 minimum are the relevant
yardsticks.

## Decision

### 1. Backend framework: FastAPI

FastAPI hosts the web surface. Templates are rendered through
`fastapi.templating.Jinja2Templates`; static assets are served via
`fastapi.staticfiles.StaticFiles`. Async-native I/O, Pydantic
validation, and automatic OpenAPI generation come for free. The
generated OpenAPI spec is used internally (developer documentation,
test scaffolding) — it is not a contract for external API consumers,
because the web variant in Phases 2–4 has no external API surface
beyond what serves its own browser frontend.

### 2. Server-Side Rendering as the default

The initial response to a navigation request is fully rendered HTML.
Interactive updates use HTMX attributes (`hx-get`, `hx-post`,
`hx-put`, `hx-delete`, `hx-swap`, `hx-target`, `hx-trigger`). Server
endpoints return either a full page or an HTML fragment, depending
on whether HTMX issued the request — the standard `HX-Request`
header distinguishes the two cases.

Templates are organised so that full pages and partials share their
rendering blocks via Jinja's `{% block %}` / `{% extends %}`
mechanism: a partial is the inner block of a page, rendered
standalone for HTMX swaps. There is no separate "API" templating
path.

### 3. Streaming via Server-Sent Events (SSE)

Shirley's chat endpoint returns `text/event-stream`. Each
`StreamEvent` from the AIService core (per ADR-0038) is serialised
to one SSE event with a typed `event:` line and a JSON `data:` line.
HTMX consumes the stream via the `htmx-ext-sse` extension and the
`sse-swap` attribute, swapping new content into the chat panel as
events arrive.

SSE is preferred over WebSockets because the traffic is one-directional
(server pushes tokens, client posts new prompts via standard HTTP),
SSE works through more proxies without configuration, and it does
not require a parallel connection-management layer in the browser.

### 4. Charts: Plotly.js

Plotly.js renders charts in the browser. Chart configurations are
assembled server-side (data array plus layout dictionary, both as
JSON) from the canonical `config/chart_theme.json` source described
in §7. The rendered HTML emits a `<div>` placeholder and a script
fragment that calls `Plotly.newPlot(...)`; the server determines the
data, the browser handles rendering and interaction (zoom, hover,
toggle, selection).

Chart types in scope for the web variant include line / area
(time-series), scatter with overlay (efficient frontier), heatmap
(correlation matrix), and bar / stacked bar (allocation views).
Plotly covers all of these natively.

### 5. Complex tables: Tabulator.js

Tabulator.js (MIT) is the default for tables that need sorting,
filtering, inline editing, hierarchical / grouped data, or
fixed-header scrolling. Pure JavaScript, no React-wrapper required.
Initial use cases: SAA asset-class tables, correlation-matrix
display, investment overviews.

AG-Grid Community is held open as an escalation option if a future
table outgrows Tabulator (notably: server-side row models for very
large datasets). The Enterprise tier of AG-Grid is not adopted for
licensing reasons.

### 6. Escalation paths beyond HTMX

When HTMX is insufficient, two explicit escalation tiers exist:

- **Alpine.js** (~15 KB) for declarative, in-DOM reactive state
  that does not warrant a full component framework: filter
  builders, multi-step forms with live validation, conditional
  field visibility, small client-only widgets. Alpine decorates
  existing HTML rather than replacing it; it composes naturally
  with HTMX.
- **Punctual React (or Preact) components**, delivered as isolated
  single-file bundles built with esbuild, embedded into Jinja
  templates as a script + container. Reserved for components that
  genuinely need component-internal state management at a level
  HTMX and Alpine cannot reach. There is no project-wide React
  build stack; each escalation is its own small bundle and is
  justified at its own call site.

Both escalations are **documentation-required**: every Alpine.js or
React-component usage is accompanied by a one-line comment naming
the reason HTMX did not suffice. The discipline prevents the
escalation from creeping into the default path by accumulation.

### 7. Design-token workflow

`config/ui_theme.json` and `config/chart_theme.json` remain the
canonical sources for design tokens (colours, font sizes, spacing,
border radii, chart palettes). The schema extension recorded in
ADR-0032 — when it lands — feeds into the same workflow.

A Python build script (e.g. `scripts/generate_theme_artifacts.py`)
reads the JSON sources and produces:

- **Web CSS:** `web/static/css/theme.css`, exporting the tokens as
  CSS custom properties on `:root` (and on per-variant scopes for
  light / corporate-blue / etc.).
- **PyQt6 stylesheets:** the existing Qt stylesheet artefacts are
  refreshed from the same tokens, either as generated files or via
  template substitution. Mechanism is settled at implementation
  time; the binding constraint is that both worlds derive from the
  same JSON source.

The script is executed via a pre-commit hook on changes to either
JSON file (or via an explicit invocation in CI), and the generated
artefacts are committed alongside their source. Drift between the
JSON and the generated artefacts is therefore caught at review,
not at runtime.

### 8. CSS strategy

Vanilla CSS with CSS custom properties is the default. No Tailwind,
no CSS-in-JS, no Sass / SCSS. The reason is the same as the
no-SPA argument: build-stack discipline. Component-level rules live
in `web/static/css/components/<name>.css`; base rules (reset,
typography, layout primitives) live in `web/static/css/base.css`;
tokens live in the generated `web/static/css/theme.css` from §7.

### 9. JavaScript distribution

Two acceptable distribution shapes exist; the choice is per-phase:

- **Phase 2:** CDN delivery for HTMX, Plotly.js, Tabulator.js, with
  Subresource Integrity hashes and pinned versions. Simpler.
- **Phase 5:** local vendor bundle under `web/static/vendor/` with
  the same pinned versions. Required for offline-capable on-premise
  deployments and friendlier to institutional CSP policies that
  prefer not to whitelist external CDNs.

The decision is reversible at any time; both shapes use the same
pinned versions, and the change is purely deployment-side.

### 10. Accessibility minimum

WCAG 2.1 Level AA is the **target** for Phase 5. Phase 2 commits to
the accessibility minima that make Phase-5 attainment cheap rather
than expensive: semantic HTML, keyboard navigability, ARIA
attributes where necessary, contrast ratios consistent with
`config/ui_theme.json`. Formal certification is a Phase-5 concern
and is not promised here.

## Rationale

- **FastAPI over Flask or Django.** Async-native is a hard
  requirement for SSE streaming under load. Pydantic integration
  matches existing project patterns. Django would import a CMS-
  shaped admin surface that PortfoliFLOW does not need; Flask is
  serviceable but does not give async streaming the same first-class
  treatment.
- **HTMX as the interactivity layer.** The 80/20 trade against an
  SPA was decided in ADR-0033. HTMX delivers it with a clean
  documentation footprint, server-side single-source-of-truth, and
  no hydration class of bugs.
- **SSE over WebSockets.** One-directional traffic plus better
  proxy compatibility plus a simpler browser-side picture (no
  reconnect loop logic at the application layer). HTMX integrates
  natively.
- **Plotly.js over Chart.js.** Heatmaps and scatter-with-overlay
  (correlation matrix, efficient frontier) are first-class in
  Plotly and awkward in Chart.js. Plotly's JSON spec maps cleanly
  to a Python dict assembled on the server from the chart-theme
  tokens.
- **Tabulator over AG-Grid as default.** MIT vs. dual-licensed.
  For a soft-pitch phase project run by a solo developer, the
  permissive license is the more robust foundation; AG-Grid
  remains available as an escalation if a future table outgrows
  Tabulator.
- **Vanilla CSS over Tailwind.** Tailwind requires a build step
  (PostCSS or Tailwind CLI). Custom properties cover the
  reusability needs at this project's scale. The cost of the
  build step would not be repaid by velocity gains.
- **Alpine before React, both behind a documentation requirement.**
  Alpine handles most of the cases where HTMX is awkward at a
  fraction of React's footprint. React stays reachable for the
  cases where component-internal state genuinely warrants it. The
  documentation requirement makes the gradient legible: a reviewer
  can tell at a glance why an escalation was made and whether the
  reason still holds.
- **JSON-sourced design tokens.** The same source has to drive Qt
  stylesheets and web CSS; CSS as the source would force the Qt
  side to parse CSS, and Qt-stylesheet syntax as the source would
  force the web side to translate Qt tokens. Neutral JSON splits
  cleanly to both targets.
- **Versioned JS via CDN initially, vendor bundle later.** CDN is
  the lowest-friction Phase-2 setup; vendor bundling is the
  CSP-friendly Phase-5 setup. Both share the same pinning
  discipline, so the swap is mechanical.

## Alternatives Considered

- **SPA (React, Vue, Svelte) as default.** Rejected in ADR-0033;
  not re-evaluated here.
- **Hotwire / Turbo instead of HTMX.** Functionally close. Rejected
  because HTMX has clearer adoption in the Python / FastAPI ecosystem
  and a more direct integration story.
- **Chart.js instead of Plotly.js.** Lighter, but the chart-type
  coverage (heatmaps, scatter with overlay) is weaker. Rejected.
- **AG-Grid Enterprise as default.** Feature-rich but
  commercial-licensed for the features that actually matter
  (server-side row model, complex pivoting). Rejected as default;
  held open as an escalation.
- **Tailwind CSS.** Velocity gains real but require a build stack
  the project does not otherwise need. Rejected.
- **CSS variables maintained directly (no JSON source).**
  Single-source-of-truth across PyQt6 and web breaks. Rejected.
- **JS via npm + bundler from day one.** Build-stack overhead
  before there is a clear gain. Rejected for Phase 2; reconsidered
  if the JS surface grows materially.
- **WebSockets for streaming.** Bidirectional channel for a
  one-directional workload, plus more proxy fragility. Rejected.

## Consequences

### Positive

- The build stack stays small and maintainable for a solo
  developer.
- HTMX is well-documented and widely adopted; Plotly.js and
  Tabulator.js are mature and permissively licensed.
- The design-token workflow keeps the desktop and web variants
  visually aligned during the strangler period.
- Server-side rendering yields predictable initial-load behaviour
  and avoids hydration-class bugs.
- Escalation paths exist for genuinely complex interaction without
  forcing them on the default.

### Negative

- HTMX's ergonomic ceiling is real. Components beyond a certain
  interaction complexity (drag-and-drop builders, live diagram
  editors, deeply nested form state) need Alpine.js or a React
  component, with the discipline overhead that implies.
- Pinned JS-library versions must be tracked manually for CVEs
  outside the Python dependency-update path. The pinning lives in
  templates (or in a vendor manifest) and is the developer's
  responsibility.
- Plotly.js is sizable (~3 MB ungzipped for the full bundle); a
  trimmed bundle (`plotly.js-basic-dist`, etc.) is an optimisation
  available later if initial-load weight becomes an issue.
- The token-generation script is a moving piece. Forgetting to run
  it is an error mode; the pre-commit hook is the mitigation, but
  the discipline still has to hold.
- Accessibility is a minimum, not a certification. Customers who
  require formal WCAG conformance trigger Phase-5 work that is not
  pre-paid here.

### Neutral / Follow-ups

- Browser support targets modern evergreen browsers. IE
  compatibility is explicitly out of scope.
- A theme switcher (light / corporate-blue / dark) is achievable
  via custom-property scopes once the schema extension of ADR-0032
  lands; not Phase-2 critical.
- A CSP / SRI pass before Phase 5 is part of the deployment-
  hardening agenda; touched here only insofar as it motivates the
  vendor-bundle option in §9.

## Implementation Notes

- **Directory layout (target):**
  - `web/main.py` — FastAPI entry point.
  - `web/routes/` — route handlers grouped by area.
  - `web/templates/` — Jinja templates, with `base.html` plus per-area
    sub-templates.
  - `web/templates/partials/` — HTMX-targeted fragments.
  - `web/static/css/` — generated tokens plus hand-written component
    CSS.
  - `web/static/js/` — bespoke JS where needed (kept small).
  - `web/static/vendor/` — optional local copies of HTMX, Plotly,
    Tabulator (Phase-5 default).
  - `scripts/generate_theme_artifacts.py` — token build script.
- **HTMX version pin** is recorded in the template that includes the
  script tag (or in a single layout include); SRI hash accompanies
  the CDN URL.
- **Plotly layout helper.** A `web/charts.py` (or equivalent) helper
  builds a Plotly layout dict from `chart_theme.json`, reusable
  across all chart endpoints; it consumes the same JSON source the
  PyQt6 chart code already reads, so theme changes propagate to
  both worlds.
- **Template convention.** Every route handler returns either a full
  page (the `HX-Request` header is absent) or a partial (header
  present); the dispatch lives in a small helper function so
  templates do not duplicate `if request.headers.get("HX-Request")`
  blocks.
- **SSE endpoint.** Returns `text/event-stream`; each AI
  `StreamEvent` from ADR-0038 is one event with a typed `event:`
  line and a JSON `data:` line; the chat panel uses
  `hx-ext="sse"` plus `sse-connect` plus `sse-swap` to consume.
- **Pre-commit hook.** Runs `scripts/generate_theme_artifacts.py`
  on changes to `config/ui_theme*.json` or
  `config/chart_theme*.json` and stages the regenerated outputs.
  Hook installation lives under the project's existing pre-commit
  setup.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:**
  - **Maintainability** — minimal build stack, single source for
    tokens, clear escalation tiers.
  - **Portability** — browser-targeted, no platform binding,
    vendor-bundle option for offline / restricted environments.
  - **Usability** — HTMX-shaped interactivity is sufficient for
    the target workflows without an SPA's overhead.
- **WCAG 2.1 Level AA** is the Phase-5 target; Phase 2 commits to
  the practice minima (semantic HTML, keyboard navigation, ARIA
  where required, contrast).
- **BAIT/VAIT.** The frontend stack itself is not directly audit-
  relevant beyond standard SBOM expectations (component versions
  documented, CVE surveillance ongoing); the substantive controls
  live in ADR-0034 through ADR-0036.

## References

- ADR-0032 (UI Theme Schema Extension) — the schema feeding the
  token workflow described here.
- ADR-0033 (Web Migration: Architectural Shift) — frame; rules
  out SPA at the strategic level.
- ADR-0034 (Persistence Backend: Postgres) — backend layer the
  frontend talks to.
- ADR-0036 (Authentication Strategy) — login flow rendered by
  this frontend.
- ADR-0038 (AIService Refactoring) — async-generator streaming
  surface consumed via SSE.
- HTMX (https://htmx.org/) — external reference.
- Plotly.js, Tabulator.js — external references.
- `config/ui_theme.json`, `config/chart_theme.json` — canonical
  design-token sources.

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-05-03 | PortfoliFLOW project owner   | Initial draft. Concretises ADR-0033's frontend-stack direction: FastAPI + Jinja + HTMX, Plotly.js for charts, Tabulator.js for complex tables, vanilla CSS with custom properties, JSON-sourced design tokens, SSE for streaming, and documented escalation paths through Alpine.js to embedded React components. |
| 2026-05-04 | PortfoliFLOW project owner   | Status moved to **Accepted**. Sub-Strang 2b lit up the FastAPI + Jinja login surface (`web/templates/login.html`, `web/templates/home.html`, `web/routes/login.py`) and the request-side dependency seam (`web/auth.py`), confirming the architectural decisions of this ADR are live. The CSS design-token build script and the rest of the frontend polish (Plotly.js / Tabulator.js / Alpine.js wiring, the SSE streaming surface) remain pending Sub-Strang 2c, but the Jinja-as-default path is now exercised by `tests/web/test_login_flow.py` and the redirect / HX-Redirect dispatch is in place. ADR moves out of Proposed because the architectural decisions are now in effect; subsequent sub-strangs implement against the accepted ADR rather than deciding it. Decider: PortfoliFLOW project owner. |
