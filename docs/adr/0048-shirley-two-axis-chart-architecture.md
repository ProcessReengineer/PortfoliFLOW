# ADR-0048: Two-Axis Chart Architecture for Shirley — Semantic Data Tools + Generic Plotly Renderer

- **Status:** Accepted
- **Date:** 2026-05-14
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, integration, ui, analytics

---

## Context

Shirley — the AI assistant on the web chat surface — can fetch
investment data (the Postgres-native investment tools from ADR-0047
work) but cannot chart it.

The existing `generate_chart` tool (`services/tools/chart_tools.py`)
reads from two sources: the in-memory `DataStore` (empty in the web
variant, per ADR-0041) or `inline_data` the model hand-types into the
tool call. For a real NAV series — hundreds of daily points —
hand-typing is infeasible and fragile, so the model correctly refuses
and offers a text "workaround" instead. A smoke test confirmed this
exact failure.

Shirley needs to chart *any* available data. The `services/chart_specs/`
package holds ~17 curated Plotly figure builders
(`build_nav_timeseries_spec`, `build_cashflows_nav_spec`, …), but they
are a **closed set against an open requirement**: as the data model
grows, the gap between "charts Shirley can produce" and "charts the
user expects" widens, and the user experiences arbitrary, unexplained
limits.

This decision is architecturally significant (criterion 1: it
establishes a data-flow boundary and an extension seam) and convention-
setting (criterion 2: the structured-data envelope is a contract other
tools must follow).

## Decision

Shirley's charting is built as **two decoupled axes**:

- **Axis 1 — semantic data-access tools.** A small, curated set of
  tools that return *semantically meaningful data bundles* as
  **structured data**. For this stage, one tool — `get_investment_data`
  — covers the four per-investment bundles: `catalogue`, `nav_series`,
  `cashflow_series`, and `return_metrics`. It returns a JSON
  **structured-data envelope** (tidy `columns` + `rows` + `meta`,
  tagged with a `__data__` discriminator). The envelope is the
  **stable contract** between the two axes.

- **Axis 2 — a single generic chart-rendering tool.** `render_chart`
  consumes the Axis-1 envelope plus a `chart_type` and presentation
  parameters, and emits a **themed Plotly figure spec**. It supports
  the six generic primitives `line` / `bar` / `grouped_bar` /
  `scatter` / `pie` / `donut`. It does **not** reproduce the curated
  figures — the model composes a generic chart from structured data,
  using `series_column` to split rows into one trace per distinct
  value (the generic equivalent of a curated plan-vs-actual overlay).

Acquisition is curated and safe; presentation is generic and does not
grow with the data model. The model's job is a simple two-step:
fetch-data tool → render tool.

The web variant goes **fully generic**: the curated `chart_specs/*`
builders stay for the web *pages* and the PyQt6 GUI, but Shirley does
not use them. Shirley's charts render as **interactive Plotly figures**
in the chat bubble — zoomable, with hover readouts, themed identically
to the web pages — rather than static matplotlib PNGs.

### Data travels by handle, not by value

The structured-data envelope does **not** travel from Axis 1 to Axis 2
as a tool-call argument. `get_investment_data` stores the envelope in a
turn-scoped, server-side cache
(`services/tools/_tool_context.py` — `store_tool_data` / `get_tool_data`
/ `clear_tool_data`) and returns the model only a short opaque
**handle** plus a compact summary (row count, column names, date span,
units). `render_chart` takes a `data_handle` argument, looks the
envelope up server-side, and renders it.

The reason: a tool call's *arguments are model-generated output*.
Passing the envelope *as an argument* would force the model to read
every row as input tokens and then **re-emit every row
token-by-token** as the `data` argument of `render_chart` — the
slowest, most expensive phase of generation, and one where the model
can corrupt a value mid-stream. The model's job is to decide *which*
data and *how* to chart it; it is not a transport layer for the data.
The handle pattern is the QT `datastore_key` principle adapted to the
web variant: the QT `generate_chart` took a `datastore_key` naming a
dataset already in the in-memory `DataStore`, and the model passed the
key, never the rows. The two axes stay fully decoupled — Axis 1
acquires, Axis 2 renders — but what flows between them is a key, not a
payload.

The cache shares the per-turn lifecycle of the tool-execution context:
the chat route clears it in the same `finally` that clears the
context, so a leaked entry from a failed turn is never visible to the
next.

## Rationale

- **Decoupling acquisition from presentation.** The rejected
  alternatives (below) both conflate the two concerns. Cutting them
  apart makes the data surface curated and safe while the rendering
  surface stays generic and constant-sized.
- **The domain grows slowly; the renderer does not grow at all.**
  Axis-1 tools track the domain entities (roughly a dozen, growing
  predictably); Axis 2 is fixed at six primitives.
- **Consistency over coverage.** A generic, consistent capability
  serves the web variant better than a curated catalogue that is
  always slightly behind the data model.
- **Stage-2 forward-compatibility.** Free matplotlib-code generation
  becomes a natural *third* render path on Axis 2: it consumes the
  same Axis-1 envelope and only swaps the render stage. In the coupled
  17-builder model it would be a foreign body.
- **Plotly over matplotlib PNG.** Reuses `services/chart_specs/base.py`'s
  themed-layout helpers (`layout_from_theme`), which are already
  Qt-free and matplotlib-free; gives visual and interaction
  consistency with the web pages; and keeps the chat surface free of a
  second rendering stack.

## Alternatives Considered

- **Alternative A — port the ~17 curated `chart_specs` builders as
  individual tools.** Rejected: it bakes a *closed* set against an
  *open* requirement. Fifty builders is unmaintainable, and worse than
  no charts because it is *inconsistent* — the user hits arbitrary,
  unexplained limits.
- **Alternative B (naive) — one tool mapping arbitrary Postgres fields
  to chart primitives.** Rejected in its raw form: it requires a
  field-selector language the model assembles ("field X of table Y
  filtered by Z"), which is effectively an ORM-over-tool-call —
  fragile for a Haiku-class model (invented field names, misunderstood
  joins, guessed enum values) and a wider data-access surface than
  necessary. The chosen design is "B with the selector language
  replaced by a curated set of semantic data bundles."
- **Keep matplotlib PNG rendering for the chat surface.** Rejected:
  static images lose the zoom/hover interactivity the web pages have,
  and a second rendering stack in the chat surface is avoidable. The
  cost paid instead is the SSE / `chat.js` migration to Plotly.

## Consequences

### Positive

- Shirley can chart any per-investment data bundle, consistently and
  predictably, with interactive Plotly figures matching the web pages.
- The structured-data envelope is a stable contract — Stage 2
  (matplotlib-code generation) plugs in as a third render path with no
  Axis-1 change. It consumes the same handle.
- `render_chart` is a pure dict transform: no rendering engine, no
  event loop, no DB access, no matplotlib, no thread concerns.
- The envelope travels by handle, not through the model — the data
  never enters the model's token stream, so it is neither read back as
  input tokens nor re-emitted as output tokens. Because of this, no
  truncation for context-budget reasons is needed: `_DATA_ROW_CAP`
  (5000) is now only a memory guard on the cached envelope, not a
  model-context concern.

### Negative

- The SSE `chart` event, the `chat.js` handler, and the
  `chart_artifact` artefact envelope had to be migrated from
  `<img src="data:image/png">` to a Plotly `spec`. Two artefact
  formats now coexist (`chart_format: "plotly" | "png"`).
- `render_chart` and `generate_chart` coexist in the registry during
  the strangler period — two chart tools, distinguished by their
  descriptions.

### Neutral / Follow-ups

- **Deferred — portfolio-level and analysis-level data bundles.**
  Portfolio aggregates (yearly cashflows, treemaps, vintage bars) and
  analysis charts (efficient frontier, correlation heatmap) are not
  built here. The analysis-level bundles are additionally blocked on
  computed-results persistence not yet being tool-reachable in
  Postgres.
- **Deferred — Stage 2: free matplotlib-code generation** as a third
  Axis-2 render path.
- **GUI-sunset cleanup.** When the PyQt6 GUI is sunset, the unused
  `chart_specs/*` builders and `generate_chart` can be removed.

## Implementation Notes

- **Axis 1:** `services/tools/investment_tools.py` — `get_investment_data(bundle, investment_name="")`.
  Reuses the `_tool_session` loop-local-engine plumbing from ADR-0047.
  The structured-data envelope contract is documented in the module
  docstring. The tool builds the envelope, stores it via
  `store_tool_data`, and returns a summary string carrying the handle
  — not the envelope itself.
- **Data handle cache:** `services/tools/_tool_context.py` —
  `store_tool_data` / `get_tool_data` / `clear_tool_data`, a
  turn-scoped server-side cache (a bounded `OrderedDict`, oldest-first
  eviction). It is the home of the structured-data envelopes between
  the two axes; the chat route clears it in the same `finally` as the
  tool-execution context.
- **Axis 2:** `services/tools/chart_tools.py` — `render_chart(...)`.
  A new tool alongside the unchanged `generate_chart` (the GUI still
  uses `generate_chart` with the DataStore — confirmed by codebase
  search — so it was not rewritten in place). It takes a `data_handle`
  argument and resolves the envelope via `get_tool_data`. `render_chart`
  themes via `services/chart_specs/base.py` and imports no matplotlib.
- **Artefact envelope:** `services/ai_service_core.py` forwards
  `chart_format` + `image_base64` + `spec` + `caption` on the
  `chart_artifact` `StreamEvent`; the large payload is stripped before
  the LLM-bound tool message.
- **Web surface:** `web/routes/chat.py` translates `chart_artifact`
  into the SSE `chart` event, branching on `chart_format`;
  `web/static/js/chat.js` renders a `plotly` spec via
  `Plotly.newPlot` (degrading to a clear inline message if
  `window.Plotly` is absent) and keeps the `png` `<img>` path
  defensively; `web/static/css/components/chat.css` gains
  `.chat-chart__plot` sizing. Plotly.js is already pinned at 2.35.2 in
  `web/templates/base.html` (ADR-0042 §4), which `chat.html` extends —
  no new script tag was needed.
- **Related tests:** `tests/assistants/test_investment_data_tools.py`,
  `tests/assistants/test_render_chart.py`, `tests/web/test_chat_sse.py`.
- **Both tools are registered `ToolClass.READ_INTERNAL`** —
  `get_investment_data` reads the tenant's own database;
  `render_chart` is a pure internal transform.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (decoupled, constant-size rendering surface), Reliability (tools
  return explanatory strings instead of raising), Usability
  (interactive, consistent charts).
- **Audit evidence:** the structured-data envelope contract is
  documented in `services/tools/investment_tools.py`; the
  acquisition path reads through the repository layer and the
  tool-execution context exactly as ADR-0047 prescribes (no new
  persistence path, no `get_data_store()` involvement); behaviour is
  pinned by the tests listed above.

## References

- Related ADRs: ADR-0041 (persistence entry points — strangler
  coexistence), ADR-0042 (Phase-3 charting architecture — Plotly.js as
  the web standard), ADR-0045 (analytics-service foundation), ADR-0047
  (tool-execution-context propagation).
- Predecessor work: the three Postgres-native investment tools
  (`list_investments` / `get_investment_detail` /
  `get_investment_nav_history`).

---

## Revision History

| Date       | Author          | Change                                   |
|------------|-----------------|------------------------------------------|
| 2026-05-14 | maintainer + AI | Initial draft, accepted on creation      |
| 2026-05-14 | maintainer + AI | Amended: the structured-data envelope travels between the two axes **by handle, not by value** (new Decision subsection "Data travels by handle, not by value"). `get_investment_data` caches the envelope server-side (`services/tools/_tool_context.py`) and returns a handle + summary; `render_chart` takes a `data_handle` and resolves the envelope from the cache. Fixes the bug where the envelope was passed as a `render_chart` argument — forcing the model to re-emit every row token-by-token. Consequence: no truncation for context-budget reasons is needed; `_DATA_ROW_CAP` is now only a memory guard. |
| 2026-05-15 | maintainer + AI | Amended: added a fifth bundle and raised the row cap. See *Amendment — portfolio_nav_series bundle and 200 k row cap* below. |
| 2026-05-20 | PortfoliFLOW project owner | Maintainer name anonymised per project convention. |

---

## Amendment — portfolio_nav_series bundle and 200 k row cap

**Date:** 2026-05-15

### Fifth bundle: `portfolio_nav_series`

`get_investment_data` now accepts a fifth bundle alongside `catalogue`,
`nav_series`, `cashflow_series`, and `return_metrics`:

- **`portfolio_nav_series`** — the NAV time series of every investment
  in the portfolio in **long form / tidy form**, one row per
  `(date, investment)` pair. Columns: `["as_of_date",
  "investment_name", "nav_value", "nav_kind"]`. Tenant-wide; ignores
  `investment_name`.

`meta` for this bundle carries `investment_count` (the number of
investments whose rows reached the envelope — i.e. legend-entry count,
which can differ from the catalogue size when some investments have
no NAV rows) and `currencies` (a sorted list of distinct currency
codes present across all rows). The model-facing summary surfaces
`"mixed currencies: …"` when more than one currency is present, so
Shirley can disclose the mix in her prose response.

#### Why long form, not multi-handle

`render_chart`'s existing `series_column` mechanism already splits a
tidy frame into one trace per distinct value of the named column. With
`series_column="investment_name"` on a `portfolio_nav_series` handle
the model produces one chart with one trace per investment without
any change to `render_chart`'s code or signature. This preserves the
*"one handle, one envelope, one chart"* symmetry the original
architecture chose deliberately, and avoids opening a parallel
multi-handle API for what is, structurally, just *concatenation* of
the same `nav_series` shape across investments.

True portfolio-level **aggregation** — e.g. a single line summing
investments' NAVs — is a separate concern (currency conversion,
plan-vs-actual scoping, weighting by ownership stake) and belongs in
the analytics layer, not in a data-access bundle. This bundle
concatenates; it does not aggregate.

### Row cap: 5 000 → 200 000

`_DATA_ROW_CAP` rises from 5 000 to 200 000. The cap is a memory guard
on the cached envelope (the rows never travel through the model per
the 2026-05-14 amendment, so it is not a token budget), and 200 k
covers realistic institutional fund-of-funds portfolios with margin —
50 investments × 10 years of daily NAVs is ~130 k rows; 200
investments × 15 years of monthly NAVs is ~36 k rows. Above 200 k is
overwhelmingly likely to be a data-import error or unresampled raw
tick data, not a legitimate use case.

When the cap fires, the envelope's `meta` block now also carries a
new `"row_count_uncapped": <int>` key recording the pre-cap row count
— so operators can see by how much the limit was overshot.
`row_count_uncapped` is **only** present when truncation fires (the
common case stays clean and the diagnostic is unambiguous when it
does appear).

### Tenant scoping

`portfolio_nav_series` inherits its tenant-scoping seam unchanged from
ADR-0047: it opens `_tool_session(ctx)` and runs every query on the
same short-lived, loop-local engine, exactly like the four existing
bundles. No new persistence path, no new context propagation.
