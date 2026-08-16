# ADR-0067: Front Office "Overview" — Portfolio Headline KPI Strip

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** PortfoliFLOW project owner
- **Tags:** frontend, ui, web, htmx, front-office, aum, kpi, overview, phase-6

---

## Context

The Front Office area (`/front-office`, ADR-0058) currently opens directly into the
Charts section. A first-time viewer therefore lands on per-investment detail with no
portfolio-level orientation. For the target audience — boutique fund-of-funds managers
and institutional allocators such as Versorgungswerke — the single most important thing
to see first is the state of the *whole* portfolio: how much is under management, and how
it is performing in the standard private-markets multiples.

Phase 5 already built the aggregation this requires. `PortfolioReviewService`
(`services/portfolio_review/portfolio_review_service.py`) exposes
`get_portfolio_overview(as_of_date=None)`, returning a `PortfolioOverviewBundle` that
carries `header_metrics: PortfolioHeaderMetrics(nav_eur, irr, tvpi, dpi)`,
`investment_count` and a resolved `as_of_date`. The Portfolio Review surface in Investor
Communication already renders these four scalars as its `.pr-strip` header. No new
calculation is needed to surface a portfolio headline in Front Office — only composition
and presentation.

One semantic point must be settled before building. In the PortfoliFLOW data model
(ADR-0055), `header_metrics.nav_eur` is the **invested book** — the sum of the latest NAVs
across investments. It is *not* AUM. **AUM** (invested capital plus operational cash) is
an authoritative, tenant-scoped daily series in the `portfolio_aum` table, read via
`PortfolioAumRepository.latest_as_of(d)` with carry-forward semantics. A headline tile
labelled "AUM" must therefore not be wired to the NAV roll-up, or it would silently
understate the figure that supervisory and actuarial reporting refer to.

A naming question also arises. The owner's first instinct was "Dashboard". For a strip of
five static figures that label over-promises — a dashboard, by convention, implies
configurable widgets, mini-charts and status signals.

---

## Decision

Add a read-only **"Overview"** section as the **first** section of the Front Office area.

### Surface and placement

- New `BaseModule` registration `Overview` (`module_name = "overview"`,
  `module_area = "front_office"`), so the section participates in the registry-ordered
  long-scroll layout, the section-indicator strip and the command palette automatically
  (ADR-0058). No sidebar entry and no new top-level area are introduced.
- The section is inserted ahead of Charts in
  `web/templates/_partials/areas/_front_office_body.html` and follows the established HTMX
  lazy-section pattern (`hx-trigger="revealed"` → `GET /api/overview/section`), mirroring
  the Charts and Portfolio Review surfaces. A single backend round-trip suffices; unlike
  Charts, there is no per-tile deferred loading, because the payload is a handful of
  scalars.

### Data sourcing — reuse, do not recompute

- IRR, TVPI, DPI, NAV (invested) and the investment count are read directly off
  `PortfolioReviewService.get_portfolio_overview()`. The two surfaces therefore agree by
  construction; the Overview can never numerically diverge from the Portfolio Review.
- AUM is read from `portfolio_aum` via `PortfolioAumRepository.latest_as_of(as_of_date)`.
- A new thin orchestrator, `FrontOfficeOverviewService`
  (`services/front_office_overview/`), composes the review service with the AUM repository
  and returns a frozen `OverviewKpis` dataclass of plain numbers. The AUM concern is **not**
  added to `PortfolioReviewService`, which stays single-purpose. The analytics layer,
  providers and repositories are untouched.

### AUM-vs-NAV labelling

- When an authoritative AUM observation exists at-or-before the as-of date: the hero tile
  is labelled **"Assets under management"** and shows the `portfolio_aum` value, with a
  secondary line breaking it into `Invested` (= NAV roll-up) and `Cash` (= AUM − invested,
  the ADR-0055 residual).
- The cash residual is suppressed when it would be negative (a stale or under-stated AUM
  row must not surface a nonsensical negative cash figure).
- When no AUM series exists for the tenant (a common state for freshly imported tenants):
  the hero falls back to the invested book, labelled **"Invested capital"**, with no cash
  line and a one-line hint inviting an AUM-series import. The hero shows the em-dash empty
  state only when there is no NAV either.

### Naming

The section is named **"Overview"**, not "Dashboard". "Dashboard" is reserved for a future
composite landing surface that may grow out of this strip (sparklines off
`bundle.multiples`, limit-coverage status from the Phase-7 engine, Heartbeat signals).
Promoting "Overview" to "Dashboard" is a deliberate future step, not an accident of v1
scope.

---

## Rationale

Reusing `get_portfolio_overview` rather than writing a parallel aggregation is the
load-bearing choice: it removes the entire class of "the two screens disagree" defects
before it can exist, and it keeps the new code in the presentation and orchestration tiers
where change is cheap. Sourcing the hero from `portfolio_aum` rather than the NAV roll-up
respects the institution's own definition of AUM and keeps the Overview consistent with the
limit-coverage engine, which already treats `portfolio_aum` as authoritative. The
invested-capital fallback keeps the surface honest and useful for tenants that have not yet
imported an AUM series, instead of showing a blank or a mislabelled figure.

---

## Consequences

### Positive

- Front Office opens with immediate portfolio orientation; the most important figures are
  the largest on the page.
- Numerical identity with Portfolio Review is guaranteed structurally, not by convention.
- The AUM/invested/cash distinction is made explicit on the primary surface, reinforcing
  the institutional data model rather than blurring it.
- Pure additive change: no existing service, repository, analytics function, provider or
  sidebar is modified, so the analytics-purity and RLS regression guards are unaffected.

### Negative

- A second surface now renders the same four scalars. This is accepted because both read
  one source; the duplication is presentational only.
- A compact EUR formatter (`€342.6M`) is introduced in the web layer. It must be unit-tested
  to avoid float-formatting drift, and it is locale-neutral English per ADR-0008 even though
  conversational German uses other conventions.

### Neutral

- v1 ships without an as-of-date control; the resolved latest-activity date is the default.
  A later iteration may add the Portfolio Review as-of form pattern.

---

## Implementation pointers

- New service: `services/front_office_overview/overview_service.py`
  (`FrontOfficeOverviewService`, `OverviewKpis`); `__init__.py` re-exports both.
- New module: `modules/front_office/overview.py`, imported from
  `modules/front_office/__init__.py` so the registry side-effect fires.
- New route: `web/routes/overview.py` (`GET /api/overview/section`), wired in `web/main.py`
  next to `charts_router`. Houses the private `_format_eur_compact` presentation helper and
  the `_build_service` wiring (seven-repository `PortfolioReviewService` plus
  `PortfolioAumRepository`).
- New templates: `_partials/overview_section_lazy.html`, `_partials/overview_section.html`;
  the section is added to `_partials/areas/_front_office_body.html` as the first entry.
- New stylesheet: `web/static/css/components/overview.css`, linked in
  `web/templates/base.html` after `portfolio_review.css`. Reuses the `.pr-strip` tokens for
  the metric cards; adds `.ov-hero` for the headline figure.
- Tests: `tests/web/test_overview_section_routes.py` (live-DB, mirrors
  `test_portfolio_review_section_routes.py`) covering the authoritative-AUM path, the
  invested-capital fallback, the empty universe, and `_format_eur_compact` thresholds.

---

## Compliance and audit relevance

**Low.** The decision adds a presentation surface above the analytics, portfolio-review and
repository layers, none of which change. It performs no new calculation: every figure is
read from `get_portfolio_overview` (already characterised against the QT reference) or from
`portfolio_aum` (ADR-0055). The `services/` purity contract (DB-free, FastAPI-free, Qt-free)
is unaffected, as are the RLS and analytics-purity regression guards. AUM is read under the
active tenant context; no cross-tenant access path is introduced. The decision is
non-confidential and is documented for traceability.

---

## Related ADRs

- ADR-0058 — Web Information Architecture (the area/section/long-scroll model this section
  plugs into)
- ADR-0055 — Cash as Residual in AUM Coverage (the authoritative `portfolio_aum` series and
  the AUM = invested + cash definition the hero tile honours)
- ADR-0045 — Charts/Statistics web migration and analytics-service foundation (the
  `PortfolioReviewService` / analytics split this surface reuses)
- ADR-0016 — Module Scope Rule (the three-line budget under which this new module
  registration falls)
- ADR-0001 — Layered architecture (route → service → repository, one-way dependencies)

---

## Revision history

| Date       | Revision | Note                                             |
| ---------- | -------- | ------------------------------------------------ |
| 2026-05-29 | 1.0      | Initial Accepted status; authored before implementation. |
| 2026-06-03 | 1.1      | Target-audience framing broadened by ADR-0074 (product scope: institutional portfolio management). Body unchanged. |
