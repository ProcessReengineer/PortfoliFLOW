# ADR-0054: SAA Surface Consolidation into Back-Office Section

- **Status:** Accepted
- **Date:** 2026-05-15
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, ui, integration

---

## Context

Phase 3 sub-streams 3c and 3d (ADR-0042) shipped the Strategic Asset
Allocation web surface as four standalone pages under `/saa`:
`GET /saa` (list view), `GET /saa/{id}` (detail / edit view),
`GET /saa/asset-classes` (catalogue manager), plus the write
endpoints (`POST /saa`, `PUT /saa/{id}/save`,
`POST /saa/{id}/activate`, `DELETE /saa/{id}`, asset-class CRUD).
Each was a full HTML page that extended `base.html` and carried its
own header strip with "Back to configurations" navigation.

Phase 6 Block 1 (ADR-0046) introduced the five-area sidebar and the
canonical `<area>/<section>` information architecture. Every other
operator-facing surface — Charts, Statistics, Portfolio Analysis,
Portfolio Review, Data Import, the Shirley chat (ADR-0051), the AI
Settings panel (ADR-0052), the Report Scraper (ADR-0053) — moved
into a section under its owning area via the
`section_body_template` pattern in `_partials/areas/_section.html`.
For SAA the Back Office body kept a placeholder pill
("CRUD live, lifts in 6F-3") pointing operators at `/saa`. The lift
itself was deferred and never executed.

Roadmap item A5 (PortfoliFLOW maintainer) now closes that loop. The
application is pre-production: bookmarks to `/saa` exist only on
the maintainer's machine, and there are no external consumers of
the URL structure. Cross-module consumers of SAA state (the
forthcoming portfolio-tracking bundle for allocation-deviation and
benchmark comparisons) will reach SAA configurations through
`services.saa.SAAService`, not over HTTP. There is no second-source
caller of the standalone HTML surface.

## Decision

PortfoliFLOW lifts SAA into a section under `/back-office#saa` and
removes the standalone `/saa` surface. The new endpoints live under
`/api/saa/*`:

| Endpoint | Purpose | Response |
|---|---|---|
| `GET    /api/saa/section`                                | Lazy-loaded section body (picker drawer + active or empty-state). Supports `?config_id={uuid}` for URL-fragment deep-linking. | HTML partial |
| `GET    /api/saa/configuration/{id}`                     | Configuration body only — picker-switch swap target.            | HTML partial |
| `GET    /api/saa/configuration/{id}/optimization`        | Optimisation chart + weights tabulator.                          | HTML partial |
| `POST   /api/saa/configuration`                          | Create configuration.                                            | JSON + `HX-Trigger: pf:saa-config-created` |
| `PUT    /api/saa/configuration/{id}`                     | Atomic save (metadata + inputs + correlations).                  | JSON |
| `POST   /api/saa/configuration/{id}/activate`            | Activation toggle.                                               | JSON + `HX-Trigger: pf:saa-config-activated` |
| `DELETE /api/saa/configuration/{id}`                     | Hard-delete.                                                     | JSON + `HX-Trigger: pf:saa-config-deleted` |
| `GET    /api/saa/asset-classes`                          | Catalogue modal partial.                                         | HTML partial |
| `POST   /api/saa/asset-classes`                          | Create asset class. Duplicate code → 409 JSON.                   | JSON + `HX-Trigger: pf:saa-asset-class-created` |
| `PUT    /api/saa/asset-classes/{id}`                     | Update display name / description.                               | JSON |
| `DELETE /api/saa/asset-classes/{id}`                     | Delete. 409 if referenced.                                       | JSON + `HX-Trigger: pf:saa-asset-class-deleted` |

The section is the canonical UI surface. Mutations signal frontend
state changes via `HX-Trigger` headers; the section stays inside
the `/back-office` shell and the frontend coordinates partial swaps
locally rather than navigating between pages.

Cross-module SAA consumers go through `SAAService` (the Python
service API), never the HTTP surface. The HTTP surface is for the
operator's web client only.

Standalone `/saa`, `/saa/{id}`, and `/saa/asset-classes` are
removed. They return 404. The five HTML templates under
`web/templates/saa/` and the route module `web/routes/saa.py` are
deleted.

## Rationale

The Phase-6 IA decision (ADR-0046) made every operator-facing
surface a section under an area. SAA was the last hold-out and the
inconsistency was visible to the operator: every other module
opened in-place under its area shell; SAA navigated away to a
separate `/saa` URL. Closing this gap is purely a structural
clean-up — the service layer, repositories, optimisation engine,
and DTOs do not change.

The application is pre-production. Lifting now, before the URL
shape has any external consumers, removes a deferred maintenance
task while it is still cheap. The forthcoming portfolio-tracking
bundle (allocation-deviation monitoring per Anlageverordnung,
benchmark comparisons) needs a stable in-process reference to the
active SAA configuration; with one canonical surface that reference
is unambiguously `SAAService.get_active_configuration()`, not a
URL-keyed view that may be re-shaped per page.

`HX-Trigger` was chosen over `HX-Redirect` for the mutation
endpoints because the section is meant to stay in the back-office
shell after every action. A redirect would force a full-page
navigation and lose the operator's scroll position and any other
section state. HTMX event coordination keeps every action local to
the section.

`/api/saa/*` was chosen as the new prefix rather than reusing
`/saa/*` to (a) signal at the URL level that the endpoints return
partials and JSON, not full pages, and (b) match the convention
already established by `/api/portfolio-analysis/*` in 6F-3.

## Alternatives Considered

- **Keep the standalone surface and redirect from `/saa` to
  `/back-office#saa`.** Rejected. A 303 redirect on the GET paths
  is possible, but the write paths (`POST /saa`, `PUT
  /saa/{id}/save`, etc.) cannot be redirected without breaking
  CSRF semantics. We would either ship a redirect for the GETs and
  silently 404 the write paths, or build a full compat shim that
  proxies into the new handlers. Both are more code than the
  delete-and-replace path, with no benefit since there are no
  external consumers.

- **Lift the section, but leave the standalone HTML pages in place
  as a fallback.** Rejected. The point of the lift is to have one
  canonical surface. A fallback would re-introduce the
  two-surface drift risk that drove the consolidation in the
  first place — every future change to the SAA edit experience
  would need to be replicated in two templates.

- **Embed the standalone pages in `<iframe>` inside the section.**
  Rejected. iframes break the shell's status-bar, section
  indicator, and command-palette wiring. They are also user-hostile
  on the keyboard-navigation axis.

- **Defer the lift until DataVault (ADR-0017) lands.** Rejected.
  DataVault is unrelated to the surface architecture; it changes
  the persistence layer, not the route shape. Waiting buys nothing
  and accumulates surface drift in the meantime.

## Consequences

### Positive

- One canonical URL for SAA: `/back-office#saa`. No two-surface
  drift between standalone pages and a section embed.
- The sidebar IA is consistent — every operator-facing surface
  lives under an Area / Section path.
- The embedded section inherits the area shell's status bar,
  section indicator, and command-palette wiring for free.
- The picker drawer (collapsible at the top of the section)
  exposes the full configuration list inline, so switching between
  configurations is a one-click affair instead of a navigation
  round-trip via `/saa` → `/saa/{id}`.
- Foundation for the forthcoming portfolio-tracking bundle:
  allocation-deviation monitoring and benchmark comparisons reach
  the active SAA via `SAAService` and never need a sibling HTTP
  surface — the consolidation locks that in by removing the URL
  surface that would otherwise tempt the design back to HTTP.

### Negative / Acceptable

- Bookmarks to `/saa` 404. Pre-production-acceptable; the test
  suite covers the absence implicitly (no test points at the old
  paths) so the removal cannot silently regress.
- The section is denser than other sections because it consolidates
  configuration list + edit + asset-class catalogue + run-optimisation
  output. Vertical structure (picker drawer collapsed → configuration
  body → optimisation output; modal for catalogue) keeps the
  cognitive load manageable; the picker drawer's `<details>` open
  state defaults to closed when an active configuration exists.
- `saa.css` is now loaded globally from `base.html` rather than via
  per-page `extra_css`. The cost is small (≈ 12 kB uncompressed)
  and the styles are scoped (`.saa-*`); no other area is affected.
- `web/static/js/saa_section.js` is substantial (≈ 890 lines)
  because it consolidates the JS previously spread across three
  page templates. It is kept as a single file to match the
  precedent set by `data_import_section.js` and `scraper.js`. If
  it grows much beyond the current scope a follow-up split would
  be appropriate; the current consolidation does not bloat the
  total JS line-count.

### Neutral / Follow-ups

- A future rename of `/api/saa/configuration/*` to
  `/api/back-office/saa/*` (aligning with `/back-office#saa`)
  would be churn for no benefit right now; no follow-up is
  scheduled.
- The picker drawer assumes the tenant has at most a handful of
  configurations. If the data shape later demands paginated or
  searchable selection, the picker can be re-implemented as a
  Tabulator-driven inline table without touching the route
  surface.

## Implementation Notes

- **Routes:** `web/routes/saa_section.py` (new). Registered in
  `web/main.py` as `app.include_router(saa_section_router,
  tags=["saa"])`. The deleted `web/routes/saa.py` is gone from
  history as of this commit.
- **Templates:** `web/templates/_partials/saa_section_lazy.html`,
  `_partials/saa_section.html`, `_partials/saa_configuration_partial.html`,
  `_partials/saa_optimization_partial.html`,
  `_partials/saa_optimization_error.html`,
  `_partials/saa_empty_state.html`,
  `_partials/saa_asset_classes_modal.html`. The Back Office body
  partial (`_partials/areas/_back_office_body.html`) was rewired
  from a placeholder pill to `section_body_template=
  "_partials/saa_section_lazy.html"`.
- **Client:** `web/static/js/saa_section.js` (new). Loaded by the
  section partial via a deferred `<script src>` tag and
  re-initialised on every HTMX swap that lands inside
  `#pf-saa-root` or `#saa-config-body`.
- **Styles:** `web/static/css/components/saa.css` extended with
  picker/drawer, large-modal, empty-state, and section-embedded
  save-bar classes; loaded globally from `base.html`.
- **Tests:** `tests/web/test_saa_routes.py` (rewritten with five new
  section-specific tests), `tests/web/test_saa_write_routes.py`,
  `tests/web/test_saa_rls.py`, and `tests/web/test_saa_audit_trail.py`
  (all path-adapted; create-path status codes changed from 303 to
  200; activate/delete now assert `HX-Trigger` instead of
  `HX-Redirect`). 40 tests pass against the compose Postgres.
- **Service layer / repositories / migration b005 / analytics
  engine:** unchanged. The lift is structural; the domain layer
  is the same code as before sub-stream 3d.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (single canonical UI surface), Usability (consistent area /
  section navigation across the application).
- **Audit evidence:** The route inventory is in
  `web/routes/saa_section.py`'s module docstring; the test suite
  in `tests/web/test_saa_*.py` exercises every endpoint including
  CSRF gating, cross-tenant RLS isolation, and audit-trail user
  attribution per ADR-0035 §6 / ADR-0036 §1d.

## References

- **ADR-0042** — Phase-3 scope (SAA-only) and the standalone
  `/saa` surface this ADR retires.
- **ADR-0046** — five-area sidebar / IA that this lift brings SAA
  into compliance with.
- **ADR-0051** — Shirley embedded in Assistants area; structural
  precedent for the section-lift pattern this ADR follows.
- **ADR-0053** — Report Scraper web surface; another section-lift
  precedent.
- Roadmap item **A5** — closed by this ADR.

---

## Revision History

| Date       | Author                     | Change         |
|------------|----------------------------|----------------|
| 2026-05-15 | PortfoliFLOW project owner | Initial draft, Accepted. |
| 2026-05-20 | PortfoliFLOW project owner | Maintainer name anonymised per project convention. |
