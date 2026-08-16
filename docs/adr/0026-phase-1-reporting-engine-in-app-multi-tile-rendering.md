# ADR-0026: Phase-1 Reporting Engine — In-App Multi-Tile Rendering

- **Status:** Accepted
- **Date:** 2026-04-27
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, ui, integration, analytics

---

## Context

ADR-0020 set out the long-term Reporting Engine as a three-layer design
(Data / Template / Style) producing PDF and PowerPoint reports per LP, with
data flowing from the planned DataVault (ADR-0017) through a Repository
layer (ADR-0018) into typed DTOs consumed by Templates and styled by a
per-client Style Layer. ADR-0017, ADR-0018, and ADR-0020 are all
`Proposed`; none of their code exists yet.

The investor-communication need is real and immediate: the user wants a
multi-tile portfolio review (one tile for the aggregate portfolio plus one
per investment) with a key-figures strip and a 2×3 grid of charts, today.
Waiting until DataVault and Repositories land before any reporting work
begins would push investor-facing value out for an indefinite period.

This ADR records the **Phase-1 implementation** that has been built as a
deliberate intermediate step toward ADR-0020. It does not redefine the
target. It explicitly says what is and is not in Phase 1, so that the
trade-offs are visible and so that the eventual ADR-0020 implementation
knows exactly which gaps it must close.

The decision has Maintainability and Functional Suitability impact (ISO
25010); Reproducibility is currently weak and is honestly flagged below.

## Decision

A reporting capability is delivered in Phase 1 with the following shape.
ADR-0020 is **not** superseded; the three-layer Data/Template/Style design
with PDF/PPTX export remains the long-term target. ADR-0026 documents the
intermediate step.

**Location.** All reporting code lives under `services/reporting/`. The
package contains no PyQt6 imports (matplotlib is used for figures, but
canvas embedding happens only in the GUI widget that consumes the
figures).

**Orchestrator.** `services/reporting/report_engine.py` exposes
`ReportEngine.build_report(report_date=None) -> list[ReportTile]`. The
engine resolves the `Stichtag` (defaults to the latest non-all-NaN row in
`navs_actual`), reads the active chart theme via `core/chart_theme.py`,
constructs one portfolio-aggregate tile and one tile per investment in
canonical Excel-row-1 order, and returns the assembled list. An empty
list is the documented "no data" outcome — this is not an error.

**`ReportTile` shape.** A frozen dataclass with:
`title` (str), `is_portfolio_level` (bool), `key_figures` (a `KeyFigures`
dataclass: `nav_eur`, `irr`, `tvpi`, `dpi` — `float | None`),
`figures` (a list of six `matplotlib.figure.Figure` instances in render
order), `figure_titles` (an aligned list of strings), and `subtitle`
(an optional one-line "Manager, Vintage, Sub-Class, Asset Class"
metadata string for per-investment tiles, empty for the portfolio
tile). Portfolio tile chart slots:
`[invested_nav, cashflow_with_nav, multiples_timeseries, country,
vintages, sector]`. Per-investment tile chart slots:
`[invested_nav, cashflow_with_nav, multiples_timeseries, total_return,
country, sector]`.

**Data layer — DataProviders.** Stateless classes under
`services/reporting/data_providers/` subclass an abstract `DataProvider`
base. Each provider takes a `ProviderContext(report_date,
all_investments, investment_filter)` and returns a normalised pandas
DataFrame (or, for `KeyFiguresProvider`, a `KeyFigures` dataclass).
Providers contain no matplotlib code and no Qt code; they read **only**
from the in-memory DataStore (ADR-0004) via `get_data_store()`. An empty
DataFrame is a normal "no data" outcome — providers never raise for
missing data. Shared calculation helpers live in
`_calculations.py`; breakdown helpers in `_breakdown.py`.

The current provider set: `CashflowProvider`, `CashflowWithNavProvider`,
`InvestedNavProvider`, `IRRProvider`, `KeyFiguresProvider`,
`MultiplesProvider`, `MultiplesTimeseriesProvider`, `CountryProvider`,
`SectorProvider`, `StrategyProvider`, `TotalReturnTimeseriesProvider`,
`VintagesProvider`.

**Render layer — ChartBuilders.** Stateless classes under
`services/reporting/chart_builders/` subclass an abstract `ChartBuilder`
base. Each builder takes a DataFrame, the active chart theme dict
(ADR-0021), and a title; it returns a themed
`matplotlib.figure.Figure`. Builders contain no Qt code and no DataStore
access; they use `core/chart_helpers.py` (`create_themed_figure`,
`apply_axes_theme`) for theming so the look matches the rest of
PortfoliFLOW. An empty input DataFrame yields a themed "No data"
placeholder figure (`make_no_data_figure`), never an exception.

The current builder set: `StackedBarBuilder`,
`StackedAreaWithLineBuilder`, `StackedBarWithLineBuilder`,
`ClusteredHorizontalBarBuilder`, `HorizontalBarBuilder`,
`LineChartBuilder`, `TreemapBuilder`, `VerticalBarBuilder`.

**Render target.** Output is rendered **in-app**. The single registered
module is `modules/investor_communication/portfolio_review.py`
(`PortfolioReview`, `module_area="investor_communication"`); its `run()`
returns `{"status": "ok", "tiles": [...]}`. The GUI widget
`gui/widgets/portfolio_review_widget.py` is the only place where
`matplotlib.figure.Figure` instances are embedded into Qt via
`FigureCanvasQTAgg`. There is no PDF and no PPTX export in Phase 1.

**No Style Layer in Phase 1.** Per-client branding is not supported.
The active chart theme (ADR-0021) provides the only styling axis. Brand
selection between the two systems' shipped variants (chart_theme.json /
chart_theme_light.json / chart_theme_print.json — see ADR-0021 history,
and ui_theme.json variants — see ADR-0025) is the closest current
analogue.

## Rationale

- **Reading from the DataStore is consistent with ADR-0004.** The
  DataStore is the documented in-process working copy of the user's
  imported data. Reporting against it is exactly the kind of read it
  was designed for. When DataVault (ADR-0017) lands, the providers'
  data source is the only thing that needs to change, mediated by
  Repositories (ADR-0018) — the providers' contract (DataFrame in,
  DataFrame out) does not.
- **Provider/Builder split mirrors Data/Template inside `services/`.**
  Phase 1 deliberately splits "what data is in the report" from "how
  the data is drawn" so the structural boundaries of ADR-0020 are
  pre-built at smaller scale. When the full three-layer engine is
  written, this is re-use, not rewrite.
- **In-app rendering is a non-decision, not an oversight.** Choosing
  a PDF library (`reportlab`, `weasyprint`, …) or a PPTX library
  (`python-pptx`) before the Style Layer's design is settled would
  lock in choices under time pressure. Deferring those choices, while
  still delivering the in-app multi-tile view, is the cheaper path.
- **No Qt in providers / builders / engine.** Phase 1 keeps
  the Service Layer separation principle of ADR-0018 even though the
  Repository layer is not yet in place. The engine is therefore
  callable from a future headless renderer or from a unit test
  without starting the Qt event loop.
- **`figure_titles` is part of the contract.** The widget needs to
  render a labelled "no data" placeholder when a builder returns an
  empty figure for one of the six slots; aligning the title list to
  the figure list is the simplest way to keep titles authoritative
  in the engine and presentational in the widget.

## Alternatives Considered

- **Wait until DataVault (ADR-0017) and Repository layer (ADR-0018)
  are implemented before any reporting work.** Rejected. Postpones
  investor-facing value indefinitely; investor reporting is one of
  PortfoliFLOW's headline use cases and is too important to gate on
  unrelated infrastructure.
- **Build the full three-layer engine of ADR-0020 directly against
  the DataStore.** Rejected. The Style Layer requires architectural
  decisions (per-client asset storage, font and image handling,
  render target selection between PDF and PPTX) that are not yet
  ripe; doing them under time pressure would lock in poor choices
  that are expensive to reverse once a report has been delivered to
  an LP.
- **Build the Phase-1 engine inside a module rather than under
  `services/reporting/`.** Rejected. That would entangle business
  logic with PyQt6 (against ADR-0011 / ADR-0018) and would force a
  rewrite when the Repository layer is added. Building Phase 1 under
  `services/` keeps the migration path additive.
- **Render to off-screen images and serve them as static PNGs.**
  Implicitly rejected — not formally evaluated. The user's wanted
  experience is interactive review inside the app; static PNGs solve
  neither that nor the long-term export goal.

## Consequences

### Positive

- Investor-facing reporting works today, against the user's actual data,
  using the same chart theme as the rest of the application.
- The Provider/Builder split inside `services/reporting/` is reusable
  when ADR-0020's full three-layer engine is built — providers will
  switch their data source from the DataStore to a Repository,
  builders are unchanged.
- Chart theming integrates cleanly via ADR-0021; brand changes flow
  through the same single-file mechanism.
- `services/reporting/` has zero PyQt6 imports, so the engine is
  callable from a future batch script or headless export pipeline
  without code edits.

### Negative

- **No per-client branding.** Style is uniform across the active
  chart theme. The Style Layer of ADR-0020 will close this gap.
- **No file export.** Reports are not exportable as PDF or PPTX in
  Phase 1; they exist only inside the running application.
- **Non-persistent, non-audited data source.** Reports read from the
  DataStore (in-memory, session-scoped, no audit fields). A report
  built today against a freshly imported workbook cannot be
  reproduced verbatim tomorrow unless the workbook is re-imported in
  the same state. Reproducibility is therefore weak — see *Compliance*
  below.
- **Per-tile fan-out has cost.** Constructing one tile per
  investment plus a portfolio aggregate runs every provider and
  builder N+1 times. For the current dataset sizes this is
  comfortable; it is not a free architectural choice and may need
  caching once portfolios grow.

### Neutral / Follow-ups

The eventual ADR-0020 implementation will close the following Phase-1
gaps:

- **Style Layer.** Per-client fonts, colours, logos, page geometry.
- **Render target.** PDF and PPTX export; today rendering is
  Qt-canvas only.
- **DataVault integration via Repositories.** Providers switch from
  reading the DataStore to reading typed DTOs from a Repository,
  unblocking reproducibility and audit fields.
- **Per-client config.** Selecting which Style applies to a given
  report at render time.
- **Reproducibility.** Once DataVault snapshots exist, a report can
  be re-rendered from a snapshot ID; today no such ID exists.

ADR-0020 is **not superseded** by this ADR — it remains the long-term
target.

## Implementation Notes

- Engine: `services/reporting/report_engine.py` (`ReportEngine`,
  `ReportTile`).
- Providers: `services/reporting/data_providers/` (abstract
  `DataProvider` and `ProviderContext` in `base.py`; concrete providers
  per file; calculation helpers in `_calculations.py`,
  `_breakdown.py`).
- Builders: `services/reporting/chart_builders/` (abstract
  `ChartBuilder` and `make_no_data_figure` in `base.py`; concrete
  builders per file).
- Module: `modules/investor_communication/portfolio_review.py`.
- GUI: `gui/widgets/portfolio_review_widget.py` (only file that
  embeds `Figure` into Qt).
- Tests: `tests/investor_communication/test_report_engine.py`,
  `tests/investor_communication/test_portfolio_review_module.py`,
  plus per-provider and per-builder tests under
  `tests/services/reporting/`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (modular Provider/Builder split, no Qt entanglement in the
  engine), Functional Suitability (the capability delivers a
  reporting Feature today), Reproducibility (currently weak —
  reports depend on DataStore session state, not on a snapshot;
  flagged honestly so it does not surprise an auditor).
- **Regulatory references:** Low for Phase 1. The capability does
  not yet write or persist anything that would be subject to
  retention or audit-trail rules; it reads ephemeral session data
  and renders to a Qt canvas. Compliance impact rises with the
  ADR-0020 implementation, when reports begin to leave the
  application as PDF / PPTX files.
- **Audit evidence:** Engine source under `services/reporting/`; the
  fact that providers and builders import nothing from `gui/` or
  `PyQt6`; the chart theme JSON used by builders (ADR-0021); the
  module registration of `PortfolioReview` in
  `modules/investor_communication/__init__.py`; tests cited above.

## References

- ADR-0004 (DataStore — Phase 1's data source)
- ADR-0011 (PyQt6 in services — `services/reporting/` deliberately
  observes the same restriction the rest of `services/` does)
- ADR-0013 (Analytics layer pure and stateless — same purity
  discipline applied to providers and builders here)
- ADR-0017 (DataVault — when implemented, becomes the data source)
- ADR-0018 (Repository layer — when implemented, mediates the
  providers' access to the DataVault)
- ADR-0020 (Planned Reporting Engine — long-term target; **not
  superseded** by this ADR)
- ADR-0021 (Chart theming — used directly by every builder)
- ADR-0025 (UI theming — sibling system at the GUI layer; the
  portfolio review widget consumes its constants for the
  surrounding UI)

---

## Revision History

| Date       | Author                       | Change        |
|------------|------------------------------|---------------|
| 2026-04-27 | PortfoliFLOW project owner   | Initial draft. Records the deliberate Phase-1 implementation under `services/reporting/` and `modules/investor_communication/portfolio_review.py`, distinct from the long-term ADR-0020 target. Code already implemented and in use. |
