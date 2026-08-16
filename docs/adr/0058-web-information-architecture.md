# ADR-0058: Web Information Architecture — Sidebar Plus Long-Scroll Areas

- **Status:** Accepted
- **Date:** 2026-05-10
- **Deciders:** PortfoliFLOW project owner
- **Tags:** frontend, ui, web, htmx, ia, phase-6

---

## Context

Phase 5's acceptance walk on 2026-05-07/08 confirmed that the web surface
of PortfoliFLOW is numerically faithful to the QT reference (24
`test_qt_consistency_*` tests green, two-decimal agreement across
analytics, chart-spec, portfolio-review and statistics layers). The walk
also surfaced a structural information-architecture gap: the web surface
at the end of Phase 5 used a flat top navigation with seven equal-rank
links, while the QT surface uses an area hierarchy that mirrors
`core/module_registry.py` — five top-level areas (Front Office, Back
Office, Admin, Investor Communication, Assistants), each containing one
or more modules.

The IA gap has three consequences:

1. The conceptual integrity of `module_registry.py`'s area-and-module
   organisation is not visible in the web surface, weakening the
   correspondence between code structure and user-facing structure.
2. Block 2 (multi-user and permissions) needs an `/admin/users` surface
   that fits the area hierarchy. Building it under a flat top navigation
   would require redoing the IA work later.
3. Block 3 (GUI migration and sunset) requires the web surface to have
   reached or surpassed the QT reference before the QT surface can
   credibly be retired. An IA gap weakens that argument.

The question is therefore not whether to fix the IA, but how. Three
pattern families were considered:

- **QT-faithful:** Two navigation columns (area sidebar + per-area
  module mini-navigation), matching the QT reference visually as well
  as functionally.
- **Modernised long-scroll:** A single area sidebar plus long-scroll
  area surfaces with sticky section headers, a section indicator strip
  on the right, and a Cmd/Ctrl+K command palette for power-user
  navigation.
- **Modernised classic:** As above but without the command palette and
  section indicator.

A clickable mock-up of all three variants was produced during the
conceptual phase (HTML file `portfoliflow-ia-mockup.html`, three
variants switchable in-page, mock content drawn from QT reference
screenshots). The project owner reviewed the mock-up and selected the
modernised long-scroll pattern as the intended IA.

The decision is recorded here for traceability and for the benefit of
Block 2 and Block 3, both of which depend on the IA established by
Block 1.

## Decision

Adopt a modernised long-scroll information architecture for the web
surface, with the following components.

**Sidebar.** A single fixed-width sidebar on the left side of the
viewport. Default width 200 pixels, collapsible to 56 pixels (icon-only)
via an explicit toggle at the bottom of the sidebar. Contains the
PortfoliFLOW logo at the top and the five areas as vertical entries.
The active area is rendered with the accent colour
(`var(--pf-accent)`), an accent-soft background and an accent-coloured
left border. The sidebar is the canonical area-selection surface; no
top navigation exists in parallel.

**Layout engine.** CSS-Grid with two columns — sidebar plus content —
in expanded state, and the same with the sidebar narrowed in collapsed
state. No JavaScript splitter. The status bar spans both columns at the
bottom of the viewport. The shell template is structured as:

```
+-------------------+----------------------------------+
|  sidebar          |  shell-main                      |
|                   |    +-- content (area template)   |
|                   |    +-- section indicator         |
+-------------------+----------------------------------+
|              status bar                              |
+------------------------------------------------------+
```

**Per-area surfaces.** Each area's content is rendered as a
long-scroll page containing all of its modules as sections, in
`module_registry.py` order. Section headers use sticky positioning
(`position: sticky; top: 0`) with a backdrop-filter for visual
separation from scrolling content. Each section has a stable HTML id
matching the module's slug (`#data-import`, `#charts`, `#statistics`,
`#portfolio-analysis` for Front Office, and so on).

**Section indicator.** A fixed-position strip on the right edge of the
content area, containing one dot per section in the active area. Hover
on a dot expands a label tooltip showing the section name. Click on a
dot anchor-scrolls to the section. A scroll-spy observer highlights
the dot for the section currently in view.

**Command palette.** A keyboard-triggered overlay opened via Cmd+K
(macOS) or Ctrl+K (other platforms). Contains a text input and a
filtered result list grouped into Areas, Sections and Actions. Arrow
keys navigate, Enter activates, Escape closes. A single backend
endpoint `/api/cmd-search` returns the filtered results as JSON. The
frontend dispatches each result type appropriately: areas trigger an
HTMX area switch, sections trigger anchor-scroll, actions trigger a
named action endpoint.

**Status bar.** A fixed strip at the bottom of the viewport, spanning
the full width.

- Left: active area name in `var(--pf-accent)`, a separator, and the
  current tenant name in secondary text colour.
- Right: a Cmd+K shortcut hint, the build SHA in monospace, and a
  config-status indicator (✓ config loaded, in success colour, or ✗
  config error in warning colour).

The status bar excludes login user (which lives elsewhere in the
sidebar starting in Block 2) and AIService status (not actionable for
the operator).

**Area-switch mechanics.** Sidebar area clicks issue HTMX requests with
`hx-target="#shell-main"` and `hx-push-url="true"`. The server returns
the new area's content fragment plus an out-of-band sidebar fragment
(`hx-swap-oob="true"`) that updates the sidebar's active-state
highlighting. A FastAPI dependency hook detects the `HX-Request` header
and selects between full layout templates and partial fragments. The
browser back button is handled by HTMX's history mechanism without
custom code, but is verified by an explicit acceptance test.

**URL structure.** One URL per area:

- `/front-office`
- `/back-office`
- `/admin`
- `/investor-communication`
- `/assistants`

Section navigation within an area uses anchor fragments
(`/front-office#charts`, `/front-office#statistics`). The legacy module
URLs (`/charts`, `/statistics`, `/portfolio-analysis`, `/import`) return
HTTP 404 after the cut-over. PortfoliFLOW is in pre-production with no
external bookmarks or documentation links, so no redirect compatibility
layer is required.

**Styling source.** All visual tokens (colours, typography, spacing,
sidebar widths, indicator dimensions, backdrop-blur values, overlay
treatments) are defined in `config/chart_theme.json` and emitted to
`web/static/css/theme.css` by `scripts/generate_theme_artifacts.py`.
New component styles are placed in a new `web/static/css/layout.css`
file that exclusively references tokens from `theme.css`. No
utility-first framework (Tailwind or equivalent) is introduced.

## Consequences

### Positive

- The `module_registry.py` area-and-module organisation is reflected in
  the user-facing IA. Code structure and UI structure correspond.
- The web surface is functionally equivalent to the QT reference and
  visually closer to the professional-financial-tool family
  (Bloomberg Terminal, Linear, Stripe Dashboard) than to a generic
  business application. This supports the target-audience aesthetic
  expectations.
- Maximum content area on every viewport size, since navigation
  consumes only one column (and zero columns when the sidebar is
  collapsed).
- The Cmd+K command palette gives keyboard-experienced operators a
  power-user navigation primitive matching contemporary professional
  software.
- Block 2 (`/admin/users` and related surfaces) and Block 3 (PyQt6
  sunset argumentation) build on a settled IA foundation.
- The decision composes with the existing theme generation pipeline
  rather than competing with it, preserving QT/web visual
  synchronisation.

### Negative

- The big-bang transition from top navigation to sidebar within 6F-1
  produces a large diff in `web/templates/_base.html`. Mitigated by
  the Phase 5 work that already established consistent template
  organisation, and by sub-stream sequencing that defers visual polish
  (6F-3, 6F-4) to later, smaller diffs.
- The HTMX-partial area-switch mechanics introduce a partial-vs-full
  response branch that must be tested for every area route. Mitigated
  by a centralised dependency hook that handles the branch in one
  place.
- The command palette adds approximately 100 lines of frontend
  JavaScript that does not have a server-side equivalent. This is an
  isolated, well-contained surface.
- Browser back-button behaviour after HTMX area switches must be
  explicitly verified, since it depends on `hx-push-url` configuration
  and not on default browser navigation. Verified in the acceptance
  walk and by an explicit test.
- The hard URL consolidation breaks any unannounced bookmarks against
  legacy module URLs. Acceptable given PortfoliFLOW's pre-production
  state.

### Neutral

- The PyQt6 surface continues to use the QT-original IA pattern
  (sidebar plus mini-navigation) until Block 3 sunsets it. The two
  surfaces visibly diverge during the Block 1 to Block 3 window. This
  divergence is intentional and bounded.

## Compliance and audit relevance

**Low.** This decision does not affect data flow, calculations,
authentication, authorisation, audit logging or any other compliance-
relevant surface. The IA reorganisation is purely presentational and
sits above the analytics, chart-spec, portfolio-review and statistics
service layers, which remain unchanged. The `services/` purity
contract (DB-free, FastAPI-free, Qt-free) is unaffected. All 24
`test_qt_consistency_*` tests must remain green throughout Block 1;
Block 1 explicitly does not modify the numerical foundation.

The decision is non-confidential. It is documented for traceability
and to give Block 2 and Block 3 a clear architectural inheritance.

## Revision history

| Date       | Revision | Note                                                 |
| ---------- | -------- | ---------------------------------------------------- |
| 2026-05-10 | 0.1      | Initial Proposed status during Block 1 conceptual phase. |
| 2026-05-20 | 1.0      | Promoted to Accepted. Renumbered from 0046 to 0058 to resolve the dual-number collision with the Region Model ADR. |

---

*Authored 2026-05-10 in the Phase 6 Block 1 mission-control sub-instance.*
