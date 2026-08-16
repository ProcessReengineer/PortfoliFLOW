# ADR-0073: Single-Investment Reviews as a Per-Investment Lazy-Loaded Stack in the Portfolio Review Section

- **Status:** Accepted
- **Date:** 2026-06-01
- **Deciders:** Soenke Pinkernelle
- **Implements roadmap item:** A7 (Single-Investment Portfolio-Review surface into the Shell-IA)
- **Related:** A1 (Portfolio Review full-build / PDF export — deferred), ADR-0037 (FastAPI/Jinja/HTMX SSR), ADR-0042 (Plotly as web charting standard, matplotlib reserved), ADR-0045 (charts/statistics web migration), ADR-0058 (web information architecture)

## Context

Phase-6 sub-stream 6F-3d delivered the **Portfolio Overview** six-tile section at
`/investor-communication#portfolio-review`. It renders the portfolio-aggregate
review only: a four-card KPI strip plus a 3×2 grid of Plotly tiles, behind a
persistent as-of-date form, lazy-loaded on first reveal.

The retired Qt desktop surface additionally rendered a **per-investment**
six-tile set for every investment in the universe, led by the portfolio
aggregate set. In the web codebase the service, analytics, and chart-spec
backbone for this already exists and is Qt-parity tested:

- `PortfolioReviewService.get_single_investment_review(investment_id, as_of_date)`
  returns a fully populated `SingleInvestmentReviewBundle` (tested in
  `tests/services/test_portfolio_review_service.py` and
  `tests/services/analytics/test_qt_consistency_portfolio_review.py`).
- The single-investment tile set is deliberately **distinct** from the portfolio
  set: tile 4 is **Total Return Index** (rebased to 100 at inception) instead of
  the portfolio's **Vintages** bar. All six spec builders are exported from
  `services.chart_specs`, including `build_total_return_index_spec`.

The only gap is the web surface. No route renders the single-investment bundle.
The legacy template `web/templates/portfolio_review/single_investment.html` is an
**orphan**: it `extends "base.html"` (the retired full-page layout, no Shell-IA
sidebar), targets dead routes (`/portfolio-review/investments/{id}`), and reads a
**stale payload contract** (`payload.header.irr_pct`, `payload.tile_specs`) that
no longer matches `SingleInvestmentReviewBundle`. Roadmap A7 correctly classifies
this as *revival, not polish*.

A PDF export of the same report (one portfolio page, then one page per
investment) is desired (roadmap A1). Path B was selected for that target:
server-side Plotly rendering via `kaleido` over the existing bundle contract,
composed into a PDF as a background job. **The PDF is deferred**; this ADR records
the forward decision only so the surface built now does not foreclose it.

## Decision

1. **Surface shape — one continuous report, not a picker.** Single-investment
   reviews are rendered as a **vertical, lazy-loaded stack appended to the
   existing Portfolio Review section** in the Investor Communication area. The
   section body becomes the full report: the portfolio-aggregate six-tile grid
   (led), followed by one per-investment six-tile grid per active investment. No
   separate section, area, pulldown picker, or drill-in modal is introduced.

2. **Lazy-load pattern — reuse the `charts_section` precedent.** The section body
   (itself lazy-loaded via `hx-trigger="revealed"`) renders one `<article>`
   placeholder per active investment. Each placeholder fetches its own six-tile
   fragment via `hx-trigger="revealed"` from a new endpoint
   `GET /api/portfolio-review/investment/{investment_id}/section`. This bounds
   per-request cost to ~6 charts per fragment instead of ~48 charts at once.

3. **Shared as-of date.** Every per-investment placeholder carries the overview's
   *resolved* as-of date in its `hx-get` URL, so the entire report shares one
   as-of date (institutional convention). Changing the date re-renders the whole
   section body and regenerates the placeholders.

4. **Ordering — by name.** Active investments are ordered by name, mirroring
   `InvestmentRepository.list_active()` and the `charts_section` precedent
   ("alphabetical order for stable presentation"). The Postgres `investments`
   model carries **no** persisted canonical-import-order column; reproducing the
   Qt "Excel-row-1" order is out of scope (see Consequences).

5. **Render-target separation (forward decision, deferred implementation).** The
   web target consumes the existing `SingleInvestmentReviewBundle` /
   `PortfolioOverviewBundle` contract per investment, rendered client-side with
   Plotly. The future PDF target (Path B) will add a batched `get_full_review()`
   loader **over the same bundle contract** and render each spec server-side via
   `kaleido` → PNG → page composition (ReportLab), executed as a background job.
   The **bundle dataclasses are the shared seam**; no batched loader is introduced
   now (it would be unused code for a lazy-loaded surface). `chart_theme_print.json`
   is reserved for that target.

6. **Legacy cleanup.** Delete the orphan
   `web/templates/portfolio_review/single_investment.html`; its tile markup is
   ported into the new fragment partial under the `_partials/` Shell-IA pattern.

## Alternatives Considered

- **Separate "Single Investment Review" section with a pulldown picker.**
  Rejected for v1: the report concept is one continuous document (portfolio led,
  investments stacked); a picker hides the per-investment sets behind a selection
  step. The picker remains a possible *additive* navigation affordance later —
  this decision does not preclude it.
- **Drill-in from the Investments list or a modal.** Rejected: breaks the
  single-continuous-report reading and overlaps the existing per-investment chart
  route `/api/charts/investment/{id}` (different chart set, different purpose).
- **Build `get_full_review()` batched loader now.** Rejected: the lazy-loaded web
  surface naturally fans out per-investment requests; a batched loader is needed
  only by the deferred PDF target. Building it now is gold-plating against the
  pragmatic-80% principle.
- **Build the PDF in the same step.** Rejected: `kaleido` is a new runtime
  dependency whose server-side rendering risks (font availability, theme parity)
  surface only in end-to-end smoke tests, and the PDF needs a background-job
  execution model distinct from the synchronous lazy-loaded web request. Bundling
  the two would contaminate a low-risk web delivery and break atomic-commit
  discipline.

## Consequences

**Positive**

- The Qt-parity per-investment report reaches the web with **no new analytics, no
  new chart specs, and no new dependencies** — only route + template + test
  wiring.
- Lazy-load bounds render cost and amortizes per-investment IRR computation across
  separate requests.
- The bundle contract is confirmed as the stable seam shared by the web surface
  and the future PDF, making Path B a render-target *addition* rather than a
  re-derivation.

**Negative / accepted debt**

- Ordering is by name, not canonical import order. If institutional reviews
  require Excel-row order, a persisted ordering column plus import-time population
  is needed. Tracked as a documented follow-up (no undocumented shortcuts).
- A full report view triggers N IRR root-finds in aggregate (one per investment
  bundle). Acceptable for current universe sizes; revisit if latency regresses.
- The section body grows with the universe; lazy-load mitigates render cost, but
  very large universes may later want pagination.

## Compliance & Audit Relevance

- **Tenant isolation unchanged.** Both endpoints run inside `tenant_context` with
  `require_session`. `get_single_investment_review` returns `None` for
  cross-tenant ids (RLS hides the row); the fragment then renders a neutral
  "review unavailable" state with HTTP 200 and does not leak row existence.
- **Read-only.** No data is written; no audit-log entries are generated.
- **Layer purity preserved.** The surface imports only `services.chart_specs`
  (Plotly, matplotlib-free per ADR-0042) and the existing service. No `matplotlib`
  or `PersistentDataStore` import is added under `web/`; regression guards
  `test_no_matplotlib_in_web` and `test_web_does_not_import_persistent_data_store`
  must remain green.

## Revision History

- **2026-06-01** — Proposed.
- **2026-06-03** — Renumbered from ADR-0069 to ADR-0073 to resolve a duplicate-number collision with `0069-shirley-back-office-analysis-tools.md` (which retains 0069); status corrected to **Accepted** to match shipped code (the per-investment lazy-loaded stack is live: `GET /api/portfolio-review/investment/{investment_id}/section`, `web/routes/portfolio_review.py`; orphan template deleted). Doc/code reconciliation pass — see `docs/_audit/doc-code-reconciliation-2026-06-03.md` §A.
