# PortfoliFLOW — Architecture

This document explains the architecture of PortfoliFLOW: not just *what* the structure is, but *why* it is the way it is. It is intended for human developers joining the project, for AI assistants generating new code, and for external reviewers (auditors, compliance, institutional investors, GP partners) who need to understand how the system is organised before they can trust its outputs.

This document is the canonical narrative description of the architecture. Three sister documents complete the picture:

- **`CLAUDE.md`** — the *runtime rules* file read automatically by Claude Code at the start of every session. A concise, prescriptive operational subset of this document: hard constraints on code generation, the abbreviated glossary used during AI sessions, Git rules, the Excel import format invariants. For depth, CLAUDE.md references this document.
- **`docs/adr/`** — the *decision log*. Numbered Architecture Decision Records (ADR-0001 onward) that capture each architecturally significant decision, the alternatives considered, and the consequences accepted. ADRs are immutable in spirit; when a decision changes, a new ADR supersedes the old one.
- **`docs/roadmap.md`** — the *steering document*. Two active categories (Loose ends / Features) plus a passive Shipped record, flat category-independent IDs (`#001`…) and P1/P2/P3 priorities; replaces the older A–D taxonomy and the Mission Control chat pattern.

Where this document and an ADR disagree, the ADR wins. Where this document and `CLAUDE.md` disagree on a *rule* (as opposed to a description), `CLAUDE.md` wins.

This document and several ADRs still cite legacy A/B/C/D roadmap IDs (e.g. `A1`, `B2`, `B5d`); these resolve to the flat `#NNN` roadmap IDs through the crosswalk in `docs/roadmap.md` and are intentionally retained rather than renumbered, since accepted ADRs and their cross-references are not rewritten.

---

## Where the project is today

PortfoliFLOW is an AI-native platform for institutional portfolio management. Its scope explicitly *includes, but is not limited to*, fund-of-funds and alternative-investment mandates; it serves institutional allocators broadly (Versorgungswerke, family offices, endowments, asset managers). ADR-0074 is the authoritative product-scope statement and governs where older records still carry the narrower fund-of-funds-boutique framing.

PortfoliFLOW migrated from a PyQt6 desktop application to a FastAPI/Jinja2/HTMX web application over Phases 1–6 of a tagged-strangler migration (ADR-0033, ADR-0039). Phase 6 (Block 1) completed in May 2026, making the web variant the primary surface; the PyQt6 GUI was then removed from the repository in July 2026 (ADR-0094 Stage 1), leaving the web variant as the sole surface. The pre-removal state is preserved at the `demo-stable-pre-qt-sunset` git tag.

The data model is Postgres-backed, multi-tenant from the schema upward, and isolated per tenant by row-level security (ADR-0034, ADR-0035). Migrations are managed by Alembic (current head: `b033`). A single operator CLI (`portfoliflow`, ADR-0040) handles bootstrap, password rotation, dev reset, and status checks.

The AI substrate — Shirley, the Tool Registry, web research, the Report Scraper — runs through a Qt-free core (`services/ai_service_core.py`, ADR-0038). The web variant and the optional Telegram bot consume the same core directly. Tool execution propagates a tenant-scoped context object (ADR-0047) so analytics tools always run against the requesting user's data.

This document describes the system as it exists *today*. Planned-but-unbuilt items are confined to the section "Planned architecture" near the end, and to the roadmap.

---

## Guiding principles

These principles predate the web migration and survived it intact. They are the reason new functionality can still be added in small, reviewable diffs.

**1. Modules are independent units.**
Each module in `modules/` can be developed, tested, replaced, or removed without affecting any other module. A module file never imports from a sibling module. If two modules need to share logic, that logic belongs in `core/`, `services/`, or `services/analytics/` — never in another module.

**2. The presentation layer knows nothing about business logic.**
The web layer under `web/` contains only presentation code: routing, rendering, request/response shaping. It discovers business logic through the ModuleRegistry and the services layer. It never contains calculations, data transformations, or schema knowledge.

**3. New functionality is always additive.**
Adding a module requires creating new files and appending at most three lines to existing ones (ADR-0016: one import in the area's `__init__.py`, one registry registration via the decorator, one entry in the relevant sidebar/route). Existing files are never modified structurally. This makes AI-assisted development safe: a code-generation prompt for a new module cannot accidentally break existing functionality.

**4. The registry is the single seam.**
`modules/module_registry.py` is the only point of contact between the presentation layer and the module layer (ADR-0003). This single seam makes it possible to test, mock, or replace the entire module layer without touching the surface, and vice versa. The same pattern applies to AI tools via `services/tool_registry.py` (ADR-0012) — the only seam between the AI service and tool implementations.

**5. Configuration is injected, never global.**
Every module receives a `Settings` instance through its constructor. There are no module-level global variables that read from the environment. Modules are testable in isolation with a mock config object.

**6. Pure analytics is dual-consumption-ready.**
The `services/analytics/` layer (ADR-0045) is pure: no database access, no Qt, no HTTP, no GUI coupling. It receives inputs as DataFrames and numpy arrays and returns structured result objects. Anything that depends on the database or the request shape is *above* analytics, never inside it. This is what allowed Phase 5 to migrate the Charts and Statistics surfaces onto the web stack without any rework of the maths.

**7. Decisions are recorded, not remembered.**
Every architecturally significant decision is captured in an ADR before the code lands. The decision log is part of the deliverable. In an audit context (BAIT/VAIT, DORA, SOC 2, ISO 25010), the ADR trail is the evidence that decisions were made deliberately rather than by accident.

---

## Canonical terminology

The terms below are binding across architecture discussions, code,
documentation, and commit messages. `CLAUDE.md` carries an
abbreviated operational version of this table for the AI runtime;
this document is the canonical source.

### Architectural vocabulary

| Term | Code mapping | Definition |
|---|---|---|
| **Area** | `module_area`, `_AREAS` | One of nine top-level operational groups, in sidebar order: Front Office, Back Office, Assistants, Planning Desk, Investor Communication, Watch Desk, Cases, Transactions, Admin (the ADR-0122 §1 order). Watch Desk was added as the sixth Area by ADR-0089; Planning Desk as the seventh by ADR-0104 §6; Cases as the eighth by ADR-0107; Transactions as the ninth by ADR-0128 §7, between Cases and Admin. ADR-0122 fixed the sidebar order above, superseding the ADR-0104 §6 order. Each has one directory under `modules/` and one URL `/{area-name}` in the web surface. |
| **Section** | Long-scroll subdivision in a web area | A section within an Area's long-scroll page, addressable via anchor (e.g. `/front-office#charts`). Multiple sections per Area. Defined by ADR-0058. |
| **Module** | `BaseModule` subclass, `@registry.register` | A registered unit of business logic assigned to exactly one Area. Discoverable via `ModuleRegistry`. Each module renders into a Section in its Area's web page. |
| **Feature** | *Planning term — not a code construct* | A user-visible capability. May span Modules, Sections, and Functions. Use in product / roadmap discussions, not for code. |
| **Function** | Python `def` / method | A Python function or method. Nothing else. Never used to mean a Feature or a Module. |
| **Service** | Class in `services/` | An integration or calculation layer that Modules and Web routes call through defined interfaces. |
| **Repository** | Class in `core/repositories/` | Async CRUD interface over one or more ORM models. Tenant-scoped, audit-aware (ADR-0034, ADR-0041). |
| **Tenant** | `Tenant` ORM row | The scoping unit of all multi-tenant data. Every domain table carries `tenant_id`, enforced by RLS (ADR-0035). |
| **Sentinel / Primary Tenant** | `PRIMARY_TENANT_ID` (= alias `SENTINEL_TENANT_ID`) | The default tenant installed by `cli/bootstrap.py` at first deployment (ADR-0040). Renamed **Primary Tenant** in ADR-0063 §7 (holds the Minathena Capital deployment, subdomain `minathena-capital` — ADR-0063 still carries the earlier pre-release demo identity; the row was renamed before public release, with migration `b012` edited in place as a documented exception); `PRIMARY_TENANT_ID` is canonical and `SENTINEL_TENANT_ID` is retained as a transitional alias in `core/tenant_constants.py`. |
| **System Tenant** | `SYSTEM_TENANT_ID = 00000000-0000-0000-0000-000000000000` | The platform-operations tenant; hosts super-admin accounts only. Subdomain `admin`. ADR-0063 §3, ADR-0064. |
| **Super-admin** | `users.is_super_admin = TRUE` | A System-Tenant user with the platform-operations role. Cannot read tenant data from the web surface (ADR-0064 §1); emergency tenant-data reads go through `portfoliflow inspect-tenant`. |
| **Tenant role** | `users.roles: TEXT[]` | One or more of `{owner, member, auditor}` (ADR-0063 §2). Owner writes domain data; Member runs and persists analytics; Auditor is read-only with tenant-scoped `audit_log` access. |
| **Tenant Resolver** | `services/tenant_resolution/` | Maps a request's `Host` header to a tenant id. Production: `SubdomainTenantResolver` (audit-engine `tenants.subdomain` lookup). ADR-0063 §1. |
| **ToolRegistry** | `services/tool_registry.py` | Single seam for AI-callable tools (ADR-0012). Every Shirley-callable tool registers with a name, schema, and Trust Class. |
| **Tool Trust Class** | Enum on registered tools | One of `READ_INTERNAL`, `WRITE_INTERNAL`, `READ_EXTERNAL_UNTRUSTED`, `EXTERNAL_EFFECT`. Gates per-turn behaviour (ADR-0022). |
| **Chart Spec** | Function under `services/chart_specs/` | A pure dict serialisable to Plotly JSON. Consumed by `web/routes/charts.py` and friends. No DB access (ADR-0045). |

### Schema vocabulary

| Term | Code mapping | Definition |
|---|---|---|
| **Investment** | `Investment` ORM, `investments` table | A single tenant-scoped investment instrument. Identified by `(tenant_id, name)`. Classified by `investment_type` and 1:1-linked to an `AssetClass`. |
| **Investment Type** | `investment_type` column | One of eight canonical values: `private_equity`, `private_debt`, `real_estate`, `infra_equity`, `listed_equity`, `listed_bonds`, `cash`, `other`. `'cash'` was added as the eighth value by ADR-0100 §1 (migration `b027`). |
| **NAV** | `InvestmentNav` ORM | Statement-day valuation for one investment. Identified by `(investment_id, as_of_date, nav_kind)`. |
| **Cashflow** | `InvestmentCashflow` ORM | A point-in-time financial event. Multiple cashflows per investment-timestamp-type combination are allowed. |
| **`nav_kind` / `flow_kind`** | column value | `'plan'` (manager projection) or `'actual'` (realised). Plan and actual series coexist. |
| **`flow_type`** | column value | One of eight canonical cashflow-type values: `capital_call`, `distribution`, `fee`, `carry`, `dividend`, `coupon`, `other`, `investor_flow`. `'investor_flow'` was added as the eighth value by ADR-0103 §5 (migration `b028`). |
| **Country (ISO 3166-1 alpha-2)** | `countries` table, `iso_code` | Two-letter country code per ISO 3166-1 alpha-2. The `XX` sentinel marks unallocated splits. The `countries` table is the single global stammtabelle in the schema. |
| **Country split** | `investment_country_weights` | Per-investment country allocation. Weights do not need to sum to 100. |
| **Region** | `Region` ORM, `region_country_memberships` | Coarse geographic grouping per the M1 Strict-Partition model (ADR-0046). Investment region splits live in `investment_region_weights`. |
| **Sector** | `Sector` ORM | Tenant-curated taxonomy with `(tenant_id, code)` UNIQUE. Each tenant has its own catalogue plus an `unclassified` sentinel. |
| **Sector split** | `investment_sector_weights` | Per-investment sector allocation. Same non-summation rule as country weights. |
| **AnlV Code** | `investments.anlv_code` | German Anlageverordnung classification on an investment. Joined to the global `anlv_categories` stammtabelle. ADR-0057. |
| **Limit / Limit Set** | `limits`, `limit_sets` ORM | Phase-7 investment-limit feature. Historised via `effective_from` (ADR-0056); `family IN ('saa', 'anlv')`. |
| **AUM** | `services/investments/aum.py` | `aum(t) = Σ nav_functional(t)` over **all** investments, cash rows included — derived, not persisted, with one shared formulation (`compute_aum`). ADR-0103 §2 retired the `portfolio_aum` series (migration `b030`) and the cash residual with it: all cash is an explicit position, so there is no unmodelled float to infer by subtraction. |
| **Net Capital Gain** | `services/analytics/investment_returns.py` | Cumulative distributions − cumulative calls + NAV, computed as a time series. The orange "ncg" line in the Cashflows tile. |
| **Total Return since Inception** | `services/analytics/investment_returns.py` | Cumulative-product return index `(1 + r_t).cumprod() * 100`. Indexed to 100 at inception. |
| **Six-tile review** | `services/portfolio_review/` | The Portfolio Review report layout: 3×2 grid of charts plus a header KPI strip. |
| **News Scraper vs. Report Scraper** | *distinct backends* | The **News Scraper** is `services/web_research/` (RSS press coverage). The **Report Scraper** is `modules/assistants/report_scraper.py` plus `services/scraper/` (GP quarterly report extraction). Use the qualified term in docs and commits — never the bare word "scraper". |

---

## Layered architecture and dependency rules

PortfoliFLOW is a strict layered system with one-way dependencies (ADR-0001). The allowed import graph:

```
                          ┌─────────────────────────┐
                          │  web/   bot/            │   surfaces
                          └─────────────┬───────────┘
                                        │
                          ┌─────────────▼───────────┐
                          │  cli/                   │   operator entry
                          └─────────────┬───────────┘
                                        │
            ┌───────────────────────────┼───────────────────────────┐
            │                           │                           │
   ┌────────▼────────┐         ┌────────▼────────┐         ┌─────────▼────────┐
   │ modules/        │         │ services/       │         │ core/            │
   │ (registered     │         │ (integrations   │         │ (infrastructure  │
   │  business       │         │  + services/    │         │  + models +      │
   │  shells)        │         │  analytics/)    │         │  repositories)   │
   └────────┬────────┘         └────────┬────────┘         └─────────┬────────┘
            │                           │                           │
            └────────── all three read from core/ ──────────────────┘
```

The hard rules:

- **`core/` imports nothing from within the project.** Only stdlib and third-party packages.
- **`services/` imports from `core/` only.** No exceptions remain: PyQt6 is not imported anywhere in the codebase — the Qt AIService adapter and its deprecation shim (`services/ai_service.py`) were removed by ADR-0094 Stage 1.
- **`services/analytics/` imports from `core/` (exceptions only) and third-party packages — nothing else.** No DB, no Qt, no HTTP.
- **`modules/` imports from `core/` and `services/` only.** Never from sibling modules.
- **`web/` imports from `core/`, `services/`, and `modules.module_registry` only.** Per ADR-0041, `web/` does **not** call `get_data_store()` or import `PersistentDataStore` — it goes through the repository layer.
- **`bot/` imports from `core/` and `services/` only.** Never from `modules/`. The Qt-free invariant is enforced by `tests/bot/test_telegram_bot.py::test_no_qt_import` (ADR-0030).
- **`cli/` imports from `core/` and `services/`** plus directly from migrations utilities. Connects to Postgres as the **superuser** (the only path in PortfoliFLOW permitted to do so — ADR-0040 §2). Application code (web, bot) always connects as the unprivileged `portfoliflow_app` role.

**Modules never import from sibling modules.** **Circular imports are a design error**, not an implementation detail to work around with lazy imports.

If a prompt or a diff asks you to violate these rules, refuse and explain why.

---

## Layer responsibilities

### `core/`

Cross-cutting infrastructure that every other layer depends on. Kept minimal and stable; changes here affect everything.

| File / subpackage | Responsibility |
|---|---|
| `base_module.py` | Abstract base class every Module subclasses. Enforces `module_name` and `module_area` at construction time (ADR-0003). |
| `config.py` | `Settings` dataclass loaded from environment / `.env`. Injected, not global. |
| `exceptions.py` | `PortfoliFlowError` hierarchy. Every project exception subclasses it (ADR-0044, supersedes ADR-0005). |
| `logging_setup.py` | Logging initialisation. Called once per process from each entry point. |
| `data_store.py` | In-memory named-DataFrame store. A Stage-2 sunset artefact (ADR-0094 §5): still consumed by the DataStore-coupled reporting engine and a few module shells; not used by `web/` per ADR-0041 (ADR-0004). |
| `persistent_data_store.py` | Strangler bridge between the in-memory DataStore and Postgres. Not imported by `web/`. |
| `models/` | SQLAlchemy 2 ORM models — `Tenant`, `User`, `Investment`, `InvestmentNav`, `InvestmentCashflow`, `InvestmentCountryWeight`, `InvestmentSectorWeight`, `InvestmentRegionWeight`, `Region`, `RegionCountryMembership`, `Country`, `Sector`, `AssetClass`, `SAAConfiguration`, `SAAAssetClassInput`, `SAACorrelation`, `DataUpload`, `AuditLog`, `DataStoreEntry`, `AnlvCategory`, `LimitSet`, `Limit`, `Benchmark`, `BenchmarkObservation`, `AssetClassBenchmarkMapping` (ADR-0061), `InvestmentBondAnalytics`, `InvestmentMaturityWeight`, `InvestmentRatingWeight` (ADR-0079), `FxRate` (ADR-0099), `InstrumentPrice`, `PositionTransaction` (ADR-0097/0098), `InvestmentIdentifier`, `MarketDataSchedule`, `IreneFinding`, `IreneSchedule`, `IreneWatchState` (ADR-0089), `Case`, `CaseEntry`, `CaseAttachment` (ADR-0107), `ScopedSetting` (ADR-0112), `Watchpoint`, `FloorCalibration` (ADR-0116), `TradeTicket`, `TradeTicketEffect` (ADR-0128). |
| `repositories/` | Repository pattern over the ORM (ADR-0018). One repository per aggregate. All queries pass through these; routes and services never touch ORM directly except inside a repository. |
| `chart_theme.py`, `ui_theme.py`, `theme_service.py` | Theme infrastructure (ADR-0021, ADR-0025, ADR-0032). |
| `tenant_constants.py` | Sentinel tenant UUID and the GUC name (`app.tenant_id`) used by RLS policies. |

### `services/`

External integrations and business services. The largest layer in line count. Organised by topic, not by Area — services are cross-cutting by definition.

| Subpackage / file | Responsibility | Authoritative ADR |
|---|---|---|
| `ai_service_core.py` | Qt-free AIService core. The streaming generator `stream_response` is the single entry point for every consumer (web routes, bot, tests). | ADR-0038 |
| `tool_registry.py` | Single seam for AI-callable tools. Every tool registers here with an explicit trust class. | ADR-0012 |
| `tool_classes.py` | The four trust classes: `READ_INTERNAL`, `WRITE_INTERNAL`, `READ_EXTERNAL_UNTRUSTED`, `EXTERNAL_EFFECT`. | ADR-0022 |
| `tools/` | Concrete AI-callable tool implementations: `chart_tools`, `datastore_tools`, `investment_tools`, `web_research_tool`, and `analysis_tools` (the back-office limit-coverage / SAA-hypothetical / portfolio-statistics tools, ADR-0069), plus the `_tool_context.py` carrier and `_async_bridge.py` helper used by ADR-0047. | ADR-0012, ADR-0022, ADR-0028, ADR-0047, ADR-0069 |
| `analytics/` | Pure, DB-free, Qt-free calculation layer. Submodules: `investment_returns`, `statistics`, `correlation`, `efficient_frontier`, `portfolio_aggregation`, `benchmark_comparison` (ADR-0061), `limit_coverage` (ADR-0055/0056/0060), plus `portfolio_optimizer` and `sample_window` folded in from the former top-level `analytics/` package (ADR-0045 fold fulfilled by ADR-0094). Irene's judgement layers live here too: `irene_delta` and `irene_floor` (ADR-0087/0088), and — since ADR-0116 §4 — the four signal-family producers `price_watch`, `fx_watch`, `nav_freshness` and `cash_coverage_watch` over the shared `signal_watch` contract. Qt-consistency tests pin parity to 1e-12. | ADR-0013, ADR-0045, ADR-0116 |
| `auth/` | Local-password backend (argon2-cffi), session repository, login audit. The auth backend is the only request path that uses the superuser engine — exclusively for the `login_audit` write. | ADR-0036 |
| `benchmark_comparison/` | `BenchmarkComparisonService` — orchestrates the per-investment-vs-benchmark, asset-class-composite, and SAA-hypothetical comparisons over the benchmark repositories and the SAA service. Converts into the tenant's functional currency at the ADR-0099 §4 boundary (`build_portfolio_fx_converter`), at all three assembly sites (`_build_investment_return_series`, `_build_composite_series`, `_build_actual_portfolio_returns`), before any return derivation — uniformly, so there is no per-site currency exception. | ADR-0061, ADR-0102 |
| `chart_specs/` | Plotly chart-spec generators for the web variant. One spec per chart type. | ADR-0045 |
| `credential_vault/` | Fernet encrypt/decrypt for the `is_secret` scoped-setting rows, plus the provider / setting taxonomy that says which keys exist and which of them are secrets. The master key (`CREDENTIAL_VAULT_MASTER_KEY`) is read from the environment only and never persisted to the database. Consumed by the scoped-setting repository and the `vault-*` CLI commands. | ADR-0112 |
| `data_normalization/` | Excel → DTO transformation pipeline. The parsing path consumed by the web `/data-import` upload (ADR-0009, ADR-0043). |
| `front_office_charts/` | `ArchetypeChartsService` — per-archetype data assembly for the Front-Office archetype-charts triplet; resolves an investment's presentation archetype and returns the pure tile inputs and KPI payload the route builds Plotly specs from. | ADR-0082 |
| `front_office_overview/` | `FrontOfficeOverviewService` — composes the Front Office "Overview" headline KPI strip and chart row from `PortfolioReviewService` plus the AUM repository. | ADR-0067, ADR-0072 |
| `fx/` | Pure FX conversion layer for the functional-currency model. `FxConverter` (a stateless value object built from a rate frame: identity short-circuit, triangulation via the reference currency, point-in-time `convert` and vectorised `convert_series`, ADR-0060-style carry-forward) plus `PortfolioFxConverter` / `build_portfolio_fx_converter`, which resolve the tenant's functional currency and assemble the conversion boundary. A missing rate raises a typed `MissingFxRateError` — there is **no silent 1:1 fallback anywhere**. Performs no I/O itself; consumed at the ADR-0099 §4 boundary by `PortfolioReviewService` and `LimitsCoverageService` (ADR-0099) and — since ADR-0102 — by `PortfolioAnalysisService`, `StatisticsService`, and `BenchmarkComparisonService`, so `services/analytics/` keeps its single-currency contract and ADR-0013 purity. One conversion idiom, five callers: ADR-0102 added consumers to this seam rather than a variant of it. | ADR-0099, ADR-0100, ADR-0102 |
| `investments/` | `InvestmentService` — orchestrates the Investment / NAV / Cashflow / weights repositories for read-aggregation DTOs and the Excel-import replace-by-investment workflow. Also hosts the position-model trio (ADR-0097/0098): `holdings.py` (pure, stdlib-only holdings derivation over the transaction ledger — purity pinned by `tests/regression/test_holdings_pure.py`), `nav_materialisation.py` (the DB-writing computed-NAV materialisation service: classify-then-write plus stranded-row deletion), and `valuation_mode.py` (the pure flip-precondition predicate shared by the flip action and the positions-panel gating). The live-ingest side adds `live_refresh.py` (the per-tenant refresh core: resolves eligible investments, drives the fetch-kind list off the capability matrix, writes through `ingest_normalized_series` as the tenant system actor with per-investment error containment), `live_schedule.py` (the cross-tenant due read behind the out-of-process tick), `market_linked.py` (the pure live-eligibility predicate — `listed_equity` / `listed_bonds` carrying a primary `isin` / `ticker` / `figi`), `credential_resolver.py` (the ADR-0095 §1 credential seam: environment-only source, explicit resolution order, per-provider fallback policy), and `cashflow_dedup_key.py` (the pure, DB-free deterministic SHA-256 dedup key over the cashflow identity tuple). | ADR-0043, ADR-0092, ADR-0093, ADR-0095, ADR-0097, ADR-0098 |
| `irene/` | Irene — the Watch Desk heartbeat. Cross-tenant cadence & scheduling, the tenant-scoped beat handler that drives one synthesis and persists surfaced findings, the `surface_finding` synthesis tool (ADR-0088), and the three delta detectors — `internal_delta` (quota), `rss_delta` (press) and `signal_delta` (the four defined signal families, ADR-0116 §4) — over the `b019` persistence layer. Since ADR-0116 the beat holds no calibration of its own: it resolves once per run through `services/watch_desk/overlay.py` and threads the result — one effective `FloorConfig` plus the resolved per-subject WARN thresholds, deltas and mutes — into the pure layers as plain arguments, so `DEFAULT_FLOOR_CONFIG` is only ever the composition input. `signal_delta` is deliberately the **stateful** half alone (watch-state upsert, acknowledged capture, delta decision, mute gate, wording): the fetch-and-produce half it used to own moved to `services/watch_desk/signal_observation.py` so the monitor could render the very same observation without a second path. | ADR-0085, ADR-0086, ADR-0087, ADR-0088, ADR-0116 |
| `limits/` | `LimitsCoverageService` — orchestrates the Phase-7 limit-coverage surface (SAA / AnlV families) over the limits, AUM, NAV, and asset-class repositories. | ADR-0055, ADR-0056, ADR-0060 |
| `market_data/` | Provider-agnostic live market-data ingest — the second producer feeding the `InvestmentService` write path, parallel to `web_research/` / `voice/`. An async `MarketDataProvider` port, a provider-blind `NormalizedSeries` DTO defined around the target tables, a declarative capability matrix (`config/market_data_capabilities.yaml`) carrying a per-provider `enabled` flag (a disabled provider is dropped from routing) and a per-provider credential policy, Yahoo (native-async) + synthetic (fixture-driven test-event) adapters, a **Bloomberg Desktop-API adapter** (fixture-validated against a synchronous `BloombergGateway` seam, bridged at exactly one `asyncio.to_thread` site; ships `enabled: false` pending a live smoke against an entitled Terminal), and deterministic OpenFIGI ISIN/ticker→FIGI resolution. No DB, LLM, or repository coupling. | ADR-0090, ADR-0091, ADR-0095 |
| `overlay/` | The pure scenario-overlay contract: an ordered list of four transformation kinds (`insert_transaction`, `repace_flows`, `market_shock`, `fx_shock`) applied to assembled plan-world frames. Three fold here as `frames → frames` executors; `fx_shock` is routed out by `partition_fx_shocks` and applied at the FX conversion seam instead, because the plan-world FX path it restates does not live in `PlanFrames`. DB-free, with its own purity guard extending the ADR-0013 idiom; no overlay ever writes book rows. | ADR-0104, ADR-0105 |
| `planning_desk/` | Scenario-input parsing (`scenario_inputs.py`) and deltas-first scenario-result assembly (`scenario_results.py`) for the Planning Desk's Scenario Analysis section — over the overlay contract and the existing engines, never a variant of them. | ADR-0104 |
| `portfolio_analysis/` | Portfolio analysis surface (efficient frontier plus constraints). Converts NAV and cashflow series into the tenant's functional currency at the ADR-0099 §4 boundary (`build_portfolio_fx_converter`) before return derivation, so the frontier is optimised on functional-currency returns and the current-portfolio weights sum in one unit. | ADR-0045, ADR-0102 |
| `portfolio_review/` | Six-tile Portfolio Review orchestrator (portfolio aggregate + per-investment tiles). | ADR-0026 |
| `reporting/` | Phase-1 in-app reporting engine — DataProviders + ChartBuilders + `ReportEngine` orchestrator. Currently rendered inline; PDF/PPTX export is roadmap item A1. | ADR-0020 (planned), ADR-0026 (Phase 1) |
| `saa/` | Strategic Asset Allocation: service, validation, seed installer. | ADR-0042, ADR-0054 |
| `scraper/` | Report Scraper backend — capabilities catalogue, message builder, JSON parser, models, service. Pure-Python; decoupled from any presentation framework. | ADR-0027, ADR-0053 |
| `statistics/` | Statistics surface (KPI strip + correlation matrix + collapsible detail tables). Converts each NAV series into the tenant's functional currency at the ADR-0099 §4 boundary (`build_portfolio_fx_converter`) before `compute_total_return_series`, so volatility and the correlation matrix are measured on functional-currency returns (FX effect included). | ADR-0045, ADR-0102 |
| `super_admin/` | `services/super_admin/operations.py` — shared platform-operations layer behind the `/super-admin/*` routes and the CLI super-admin subcommands (one implementation, two consumers; audit-in-transaction). | ADR-0064 |
| `telegram_pairing.py` | The in-process pairing-code store that binds a Telegram chat to a user (`/pair <code>`). The one module both `web/` and `bot/` import, which is why it lives here rather than in either surface. | ADR-0112 §5 |
| `tenant_resolution/` | `SubdomainTenantResolver` (production, audit-engine `tenants.subdomain` lookup) and `ExplicitHostHeaderResolver` (tests). Maps a request `Host` header to a tenant id. | ADR-0063 |
| `voice/` | Channel-agnostic speech-to-text / text-to-speech service for Shirley behind a `VoiceProvider` Protocol with one OpenAI adapter. Turn-based STT pre-processor and post-completion TTS, strict no-silent-fallback discipline. Since ADR-0118 the configuration is resolved **per request** through the scoped-settings apparatus, with the `VOICE_*` variables as the application-scope links. | ADR-0076, ADR-0118 |
| `watch_desk/` | The Watch Desk's impure half, between the `b033` repositories and the pure judgement layers. `calibration.py` is the sanctioned calibration write path (composes a desired configuration over the code defaults, validates it whole through the `FloorConfig` constructor **and** the pinned invariants, reduces it to deviations, persists) plus `effective_floor_config`. `overlay.py` is the single per-tenant resolution — effective `FloorConfig` ⊕ tenant WARN default ⊕ per-subject overlays ⊕ effective signal watchpoints — that **both** the beat and the monitor route consume, so a finding's classification and the row the operator reads cannot disagree about the threshold they were measured against; a stored revision that no longer composes fails the tenant's beat loudly rather than degrading to the defaults. `signal_observation.py` is the same promise one layer down, applied to data access: one batched fetch per signal family, then the family's pure producer, **read-only** and shared by the beat and the monitor — so a monitor row is literally the number the next beat will classify rather than a recomputation that agrees today. `seeding.py` is the idempotent default-watchpoint installer and the single home of the default display names and subject-key spellings the add flows reuse. Not to be confused with `services/overlay/` — that is the ADR-0104 scenario-transformation contract, an unrelated sense of the word. | ADR-0116 |
| `web_research/` | Allowlisted, RSS-based news research behind the Web Research AI tool. Includes the Fetcher LLM that wraps untrusted external content in `<external_content trust="untrusted">…</external_content>` before it reaches Shirley. | ADR-0023, ADR-0024 |

`services/analytics/` is **not** a new top-level Area in the canonical glossary sense — it is a sub-module tree under `services/`, accessible from `modules/` and `web/` via normal imports. The former top-level `analytics/` package was folded into it by ADR-0094, fulfilling the fold planned in ADR-0045.

### `modules/`

The registered-business-logic layer. Organised into the nine Areas that mirror a portfolio management company's operational structure (Watch Desk added as the sixth Area by ADR-0089, Planning Desk as the seventh by ADR-0104 §6, Cases as the eighth by ADR-0107, Transactions as the ninth by ADR-0128 §7). Each module is a `BaseModule` subclass registered via `@registry.register`.

| Area | Directory | Modules (current) |
|---|---|---|
| Front Office | `modules/front_office/` | `overview`, `data_import` |
| Watch Desk | `modules/watch_desk/` | `briefing`, `journal`, `calibration` |
| Cases | `modules/cases/` | `open_cases`, `recently_closed`, `archive` |
| Planning Desk | `modules/planning_desk/` | `cash_flow_planning`, `scenario_analysis` |
| Back Office | `modules/back_office/` | `saa`, `benchmarks_attribution`, `limits` |
| Admin | `modules/admin/` | `application_settings` |
| Investor Communication | `modules/investor_communication/` | `portfolio_review` |
| Assistants | `modules/assistants/` | `shirley`, `ai_settings`, `report_scraper` |

A module's public interface is its `run()` method plus any typed public methods it declares. Internal helpers and private state are implementation details.

Many of the modules in the web variant have become thin "registry shells" — they exist so the module appears in the registry and inherits the area/glossary discipline, but the heavy lifting lives in the corresponding service under `services/`. Examples: `modules/assistants/report_scraper.py` shells over `services/scraper/` (ADR-0027); `modules/investor_communication/portfolio_review.py` shells over `services/reporting/` and `services/portfolio_review/` (ADR-0026); `modules/back_office/saa.py` shells over `services/saa/` (ADR-0054). This is deliberate — it keeps the registry as the single seam while putting reusable logic where it can be consumed by tools, the bot, and the web routes too.

### `cli/`

The operator CLI — `portfoliflow ...` — for deployment and maintenance tasks the running application is not responsible for (ADR-0040). Subcommands:

| Command | Purpose |
|---|---|
| `portfoliflow bootstrap` | Idempotent sentinel-tenant + sentinel-user initialisation. Installs seed data (SAA seeds, unclassified + default asset classes, unclassified sector, default regions, market-data system actor + disabled schedule). |
| `portfoliflow set-password` | Rotate the password of an existing user. Invalidates active sessions on rotation (OWASP guidance). |
| `portfoliflow reset-dev --confirm` | Destructive, dev-only: truncate every domain table, re-bootstrap the sentinel and re-run the full bootstrap seed pipeline (seed parity with `bootstrap`). Schema state (`alembic_version`) is preserved. |
| `portfoliflow status` | Non-destructive diagnostic snapshot: schema head, sentinel state, user count, AI configuration. |
| `portfoliflow irene-tick` | Tenant-blind Irene heartbeat: beat every tenant whose `irene_schedule` is due. Fired by a systemd timer (ADR-0086). |
| `portfoliflow market-data-tick` | Tenant-blind live-import tick: refresh every tenant whose `market_data_schedule` is due, mirroring the Irene tick 1:1 (ADR-0093). No AI dependency; `--tenant` / `--provider` test-seam flags. Fired by a systemd timer. |

The CLI connects to Postgres as the superuser (`DATABASE_URL_SUPERUSER`). Application code never does this. The two periodic ticks (`irene-tick`, `market-data-tick`) are one-shot commands driven by external systemd timers; per-tenant cadence lives in the database, and each claims a tenant with a domain-separated `pg_advisory_lock` so overlapping ticks never double-run a tenant.

### `db/`

The database schema and infrastructure: `init/` for container-init SQL (creates the `portfoliflow_app` role on the first Postgres start), `migrations/` for Alembic versions (`b001` to `b034` at time of writing), `alembic.ini`, `postgresql.conf`, and `README.md`. The `init/` SQL is run by the Postgres container entry point on the first start against an empty data volume and never re-runs, so re-triggering it means discarding the volume — `scripts/db-reset.sh`; see `db/README.md`. Migrations are the only path that changes schema; ORM-side `Base.metadata.create_all` is never used in this project.

### `web/`

The FastAPI + Jinja2 + HTMX presentation layer (ADR-0037). Server-side rendering by default; HTMX swaps for incremental interactivity. No SPA framework, no client-side state container, no build pipeline beyond the theme generator.

| Subdirectory / file | Responsibility |
|---|---|
| `main.py` | App factory + the `portfoliflow-web` runner. Manages two engines: `app.state.engine` bound to `DATABASE_URL` (unprivileged) for all request handling, and `app.state.audit_engine` bound to `DATABASE_URL_SUPERUSER` exclusively for `login_audit` writes. Reads `OPENROUTER_BASE_URL` / `OPENROUTER_API_KEY` / `SHIRLEY_MODEL` at startup and configures the AIService core. |
| `auth.py`, `dependencies.py`, `settings.py`, `shell.py` | Session-cookie auth, FastAPI dependencies, app settings, sidebar/area resolution. |
| `routes/` | One module per area / section: `areas`, `benchmarks_attribution`, `cases`, `charts`, `chat`, `cmd_search`, `data_import`, `health`, `investments`, `limits`, `login`, `market_data`, `overview`, `planning_desk`, `portfolio_analysis`, `portfolio_review`, `provider_credentials`, `saa_section`, `scraper`, `shell`, `statistics`, `super_admin`, `watch_desk`. |
| `templates/` | Jinja2 templates. `base.html` is the shell; `_partials/areas/*` are the per-area body partials; `_partials/*_section*.html` are the section partials loaded via HTMX lazy-load; `areas/*` are the area-level views; `investments/`, `portfolio_review/` are domain-specific template trees. |
| `static/css/` | One `theme.css` (generated from `config/*.json`) plus `base.css`, `layout.css`, and one component CSS file per surface in `components/`. |
| `static/js/` | Vanilla JS for chat, data-import section, SAA section, scraper, section nav. No bundler. |

The web variant follows a section-pattern (ADR-0058): an Area is a route (e.g. `/front-office`); a Section inside the Area is a fragment (e.g. `/front-office#charts`). Sections lazy-load via HTMX from a `_section_lazy.html` partial. Shirley is embedded as one such section under `/assistants#shirley` (ADR-0051). SAA was consolidated from standalone pages into a section under `/back-office#saa` (ADR-0054). The Providers & Credentials surface — the write path for scoped settings and credentials — is under `/admin#providers-credentials` (ADR-0112 §6); it replaced the ADR-0052 AI Settings section, which mutated the running `AIServiceCore` singleton and persisted nothing.

CSRF is session-bound and embedded in `base.html` as a meta tag for fetch-based mutating requests; form-based POSTs continue to embed the token as a hidden input.

### `bot/`

The Telegram bot — a non-GUI consumer of `AIServiceCore.stream_response` (ADR-0030). Runs in a daemon thread on its own asyncio event loop inside the same PortfoliFLOW process. May import from `core/` and `services/` only; never from `modules/`. The Qt-free invariant is enforced by `tests/bot/test_telegram_bot.py::test_no_qt_import`. aiogram is an optional dependency under the `bot` extra in `pyproject.toml`.

Since ADR-0112 §5 that one thread is **multiplexed**: `bot/token_discovery.py` scans every tenant's stored `telegram.bot_token` at start (a cross-tenant read on the superuser engine, the fourth sanctioned consumer of it), and each discovered token gets its own `Bot`, `Dispatcher` and supervised polling task on the shared loop. One dispatcher's dead token never touches the others. Authorisation is a **pairing** binding, not a whitelist: a user-scope `telegram.chat_id` row written by `/pair <code>`, redeemed against the in-process store `services/telegram_pairing.py` (the one module both `web/` and `bot/` import, which is why it lives in `services/`). A paired turn resolves its LLM *as that user*. Because each bot polls via `getUpdates` — one consumer per token — the single-uvicorn-worker constraint is now load-bearing for N bots; see `docs/deploy/telegram-multi-bot.md`.

The bot constructs its own `AIServiceCore` instance, distinct from the web surface's. The shared ToolRegistry and the process-wide `_TURN_LOCK` in `services.ai_service_core` are shared across both cores, so trust-class gating and turn serialisation (ADR-0031) still work correctly.

---

## Multi-tenancy and row-level security

Every domain table has a `tenant_id` column (ADR-0034, ADR-0035). RLS policies on each table read `current_setting('app.tenant_id')` and filter rows accordingly. The request lifecycle is:

1. The session cookie identifies the user; the user's `tenant_id` is looked up.
2. A FastAPI dependency opens a connection on the unprivileged engine and runs `SELECT set_config('app.tenant_id', :tid, true)` as the first statement in the transaction.
3. Every subsequent query in that transaction sees only rows belonging to that tenant — RLS does the filtering, application code does not need to add `WHERE tenant_id = ?` predicates.
4. The repository layer (`core/repositories/`) wraps the engine and exposes domain-typed read/write methods. A pytest regression suite (`tests/regression/test_rls_schema_invariants.py`, `tests/repositories/test_*_audit_and_isolation.py`) verifies that every table has an active RLS policy and that a query under a different `tenant_id` returns zero rows from another tenant.

The bootstrap CLI (ADR-0040) creates the Primary Tenant + its owner and the System Tenant + super-admin idempotently. Further tenants are provisioned at runtime by `portfoliflow create-tenant` (and the equivalent super-admin route), which installs the same per-tenant default seed as bootstrap — the `unclassified` asset class, the Phase-7 default asset-class catalogue, the `unclassified` sector, the default regions, the SAA seeds, and the market-data system actor + a disabled `market_data_schedule` row (ADR-0093) — so every tenant is import-ready on creation (ADR-0077). Because that seed installer runs on the privileged, RLS-bypassing superuser connection, `tenant_context` (`core/repositories/_session.py`) switches the transaction to the unprivileged application role after setting `app.tenant_id`, making RLS enforcement true-by-construction regardless of the connecting role (ADR-0078). Subdomain tenant routing and the `owner/member/auditor` role model shipped with ADR-0063 (migration `b012`), and the CLI-driven super-admin platform-operations surface shipped with ADR-0064 (migrations `b013`/`b014`). The remaining multi-user work under roadmap B1 is the per-action tool-trust overlay (B1c) and the tenant-owner user-management UI (B1f); B1 is therefore `in-progress`, not complete.

The auth backend's `login_audit` writes are the only exception to "request handlers use the unprivileged engine". The audit table sits outside the RLS-filtered surface so it can record failed logins (including for non-existent users); writes go through the superuser engine bound to `app.state.audit_engine`. The asymmetry is documented in `services/auth/local_password.py`.

---

## Data flow: an end-to-end example

A typical web request — *"render the Portfolio Review section at `/investor-communication#portfolio-review` for the signed-in user"* — exercises every layer. The flow:

```
Browser
  │
  │ GET /investor-communication/portfolio-review/_section
  ▼
web/routes/portfolio_review.py
  │ Resolve session → user_id → tenant_id (FastAPI dependency, web/auth.py)
  │ Open a connection on app.state.engine, SET app.tenant_id = <tid>
  ▼
services/portfolio_review/portfolio_review_service.py
  │ Orchestrates repository reads
  ▼
core/repositories/  (investment, investment_nav, investment_cashflow,
                    investment_country_weights, investment_sector_weights,
                    investment_region_weights)
  │ Returns DTOs; RLS policies filter by tenant transparently
  ▼
services/reporting/report_engine.py  (uses services/reporting/data_providers/*)
  │ Aggregates DTOs into ReportTile records (key figures + figures + titles)
  │ Calls services/analytics/portfolio_aggregation, investment_returns, statistics
  ▼
services/chart_specs/*   (portfolio_review_*, investment_*)
  │ Produces Plotly chart specs from the aggregated data
  ▼
web/templates/_partials/portfolio_review_section.html
  │ Renders the six tiles as HTML, embeds the Plotly JSON inline
  ▼
Browser  (HTMX swaps the section into the area body)
```

Every layer in this chain is independently testable. The repositories are testable against the live Postgres container with a per-tenant fixture. The service layer is testable against in-memory DTOs. The analytics is testable with synthetic numpy/pandas inputs and pinned to 1e-12 against the Qt reference implementation. The chart-spec generators are testable with golden-JSON snapshots. The route is testable with `httpx.AsyncClient` against the FastAPI app.

This is what "dual-consumption-ready" means in practice: the same `services/analytics/` and `services/reporting/` code drives the web variant today and would drive any future surface (a CLI report, a PDF exporter, a different presentation framework) without changes.

---

## The module lifecycle

A module goes through four stages.

**1. Specification.** Before any code is written, a module spec is filled in using `docs/module_spec_template.md`. The spec defines inputs, outputs, dependencies, public API, and the area it belongs to. The spec is the prompt basis for AI-assisted code generation.

**2. Scaffold.** The module file is created with the correct class structure, typed method signatures, and Google-style docstrings (ADR-0007), but no implementation. `run()` returns `{"status": "not_implemented"}`. The module is registered and appears in the area's `__init__.py`. At this point the module exists structurally, can be discovered by the registry, and is testable for its metadata.

**3. Implementation.** Business logic is added — usually by delegating to a service under `services/` or to analytics under `services/analytics/`. Each method is implemented and tested independently. The module can be developed in isolation without running a surface.

**4. Surface integration.** If the module needs a web surface, the corresponding pieces land under `web/`: one route module under `web/routes/`, one section partial under `web/templates/_partials/`, one component CSS file under `web/static/css/components/`. The route resolves session → tenant, calls the service layer, renders the template. This is the only step that touches the presentation layer.

Step 4 is *not* required — a module that is only consumed by AI tools (a `READ_INTERNAL` data lookup, for instance) needs no surface at all. The Tool Registry is its own seam.

The **three-line rule** (ADR-0016) applies across all four stages: adding a module touches at most three lines in existing files. If your diff is larger than that, you are modifying an existing module — not adding a new one — and the change must be justified accordingly.

---

## AI-assisted development workflow

PortfoliFLOW is built by a single developer with AI assistance, in a human-in-the-loop model where the AI generates code for individual modules and the human reviews and commits each piece (ADR-0015). The workflow rests on three artefacts:

- **`CLAUDE.md`** — read automatically by Claude Code at the start of every session. Defines the rules and conventions. The AI cannot drift outside these rules without the human noticing in review.
- **A Repomix snapshot** — a single XML file containing the whole repository, generated on demand. The snapshot is the canonical context for architectural reviews (Opus) and for implementation prompts (Sonnet/Claude Code).
- **A module spec** — filled out from `docs/module_spec_template.md` for any non-trivial change. The spec is the prompt.

The workflow for adding a module:

1. Commit-checkpoint the working tree (ADR-0014: Conventional Commits + checkpoint discipline before AI sessions). The diff after the session should be reviewable against this checkpoint.
2. Fill in the module spec.
3. Generate a Repomix snapshot.
4. Open a concept discussion with Opus on the snapshot + the spec. The output of this step is a Claude-Code-ready markdown prompt that pins the design decision.
5. Run the prompt in Claude Code. The AI generates the new module file(s) and the three append-only lines in existing files.
6. The human reviews the diff against the checkpoint. It should touch at most three existing lines plus the new files.
7. Tests are run. The module appears in the registry; if it has a surface, it appears in the route table.

The roadmap (`docs/roadmap.md`) is the steering document above this workflow. Each roadmap item is portioned so it can be the starting point of one implementation chat — Roadmap line + Repomix + (optional) concept discussion = one focused session.

---

## AI service architecture

The AI substrate has three concentric layers.

**1. AIService core (`services/ai_service_core.py`, ADR-0038).** A Qt-free, asyncio-native streaming generator. `AIServiceCore.stream_response(conversation, model)` yields typed `StreamEvent` records: `token`, `tool_call`, `tool_result`, `chart_artifact`, `error`, `done`. The core knows nothing about Qt, FastAPI, or aiogram. It owns connection state (`DISCONNECTED` / `CONNECTED` / `STREAMING`) and the system prompt loaded from `Soul_Shirley.md`. It is a singleton per process.

**2. Adapters.** Two adapters wrap the core for the two surfaces:

- **`web/routes/chat.py` + SSE** — translates `StreamEvent` records into typed Server-Sent Events served at `/chat/stream/<turn_id>`. The HTMX shell swaps tokens in incrementally.
- **`bot/telegram_bot.py`** — translates `StreamEvent` records into Telegram messages, accumulating tokens until a sentence boundary, posting chart artefacts as photos. Runs on the bot's own asyncio loop in a daemon thread, one dispatcher per tenant (ADR-0112 §5); handler state, including the conversation history, is keyed by `(tenant_id, chat_id)`, since one private chat id is the same for every tenant's bot.

**3. Tool Registry (`services/tool_registry.py`, ADR-0012).** The single seam for AI-callable tools. Every tool registers with an explicit trust class (`READ_INTERNAL` / `WRITE_INTERNAL` / `READ_EXTERNAL_UNTRUSTED` / `EXTERNAL_EFFECT`, ADR-0022). The trust class drives per-turn gating: once a `READ_EXTERNAL_UNTRUSTED` tool has fired in a turn, `WRITE_INTERNAL` and `EXTERNAL_EFFECT` tools are locked for the rest of that turn. Content from untrusted tools must pass through the Fetcher LLM and be wrapped in `<external_content trust="untrusted">…</external_content>` before reaching Shirley's conversation.

**Tool execution context (ADR-0047).** When the web variant invokes a tool, it passes a tenant-scoped `ToolContext` carrying `tenant_id` and the SQLAlchemy engine. The tool implementation under `services/tools/` reads the context via a contextvar and opens a connection that sets `app.tenant_id` before any query — so analytics tools see exactly what the requesting user sees on screen. The bot side passes the sentinel tenant.

**Process-wide turn lock (ADR-0031).** A module-level `threading.Lock` in `services.ai_service_core` serialises turns across all surfaces. Concurrent turns from the web surface and the bot wait for each other. This is an interim measure; a per-user lock will replace it when multi-user lands (roadmap B1).

The four hard rules carried forward from the ADRs (do not violate even if the ADR has not been re-read):

- Never instantiate an LLM client directly. Route through `get_ai_service_core()`.
- Never bypass the ToolRegistry. Register every AI-callable tool via `get_tool_registry()` with a trust class.
- Content from `READ_EXTERNAL_UNTRUSTED` tools must pass through the Fetcher LLM and be wrapped in `<external_content trust="untrusted">…</external_content>` before reaching Shirley.
- Once a `READ_EXTERNAL_UNTRUSTED` tool has fired in a turn, `WRITE_INTERNAL` and `EXTERNAL_EFFECT` tools are locked for the rest of that turn.

---

## Domain schema

The Postgres schema is built migration by migration; `b001` through `b033` at time of writing. The high-level shape:

**Identity and tenancy.** `tenants`, `users`, `sessions`, `login_audit`. The `users` table carries `password_hash` (argon2), `is_active`, `is_tenant_owner`. Sessions are server-side; the session cookie is the only client-side state.

**Audit substrate (ADR-0019).** Every domain table has `created_by`, `created_at`, `modified_by`, `modified_at`, `source`. A trigger fires on insert/update to populate these from the `app.tenant_id` and `app.user_id` GUCs set per request.

**Reference data.**
- `countries` (ISO 3166-1 alpha-2; global, not per-tenant; carries `region_default`). The `XX` sentinel marks unallocated country splits.
- `sectors` (per-tenant; `(tenant_id, code)` UNIQUE; each tenant gets an `unclassified` sentinel at bootstrap).
- `regions` + `region_country_memberships` (M1 Strict-Partition; pre-seeded with 12 regions at bootstrap, per ADR-0046).
- `asset_classes` (per-tenant; each tenant gets an `unclassified` fallback at bootstrap, per ADR-0043).

**Investment domain (ADR-0043).** `investments` is the aggregate root, keyed by `(tenant_id, name)`. It carries `investment_type` (one of eight canonical values: `private_equity`, `private_debt`, `real_estate`, `infra_equity`, `listed_equity`, `listed_bonds`, `cash`, `other` — `'cash'` added by ADR-0100 §1, migration `b027`) and a 1:1 link to an `AssetClass`. Three child tables hang off it:

- `investment_navs` — per `(investment_id, as_of_date, nav_kind)` with `nav_kind ∈ {'plan', 'actual'}`. Plan and actual coexist; neither overwrites the other.
- `investment_cashflows` — per `(investment_id, ts, flow_kind, flow_type)`; **no UNIQUE constraint**, because multiple capital calls or fees can share a day. `flow_type ∈ {'capital_call', 'distribution', 'fee', 'carry', 'dividend', 'coupon', 'other', 'investor_flow'}` (`'investor_flow'` added by ADR-0103 §5, migration `b028`).
- Three composition-weight tables — `investment_country_weights`, `investment_sector_weights`, `investment_region_weights`. Each is a per-investment percentage allocation; **weights for one investment do not have to sum to 100** — partial allocation is valid. These were **historised to time-series** by ADR-0080 (migration `b017`): each row now carries an `as_of_date` and a `basis` (`'reported'` | `'computed'`) discriminator in its natural key, so composition snapshots accumulate over time instead of overwriting the prior generation. The non-summation rule holds per snapshot; the pure aggregation layer stays indifferent to historisation, and the repository resolves "which snapshot is current".

**Region model (M1 Strict-Partition, ADR-0046).** Region weights live in `investment_region_weights`. Country weights in `investment_country_weights` remain reserved for ISO-granular sources (e.g. the GP report scraper). Excel imports populate region weights strictly: unknown region labels are hard import errors, not soft fallbacks.

**Liquid-asset archetypes (ADR-0079, migrations `b016`–`b018`).** The per-investment data layer for the liquid archetypes (fixed income, listed equity) landed in June 2026. Migration `b016` added three tenant-scoped **time-series** tables for the Fixed-Income archetype — `investment_bond_analytics` (`ytm`, `eff_duration`, `oas`, `convexity` per `(investment_id, as_of_date)`), `investment_rating_weight` (credit-rating distribution over eight canonical buckets), and `investment_maturity_weight` (maturity ladder over six canonical buckets) — each keyed on `as_of_date` and carrying the same `basis` (`'reported'` | `'computed'`) discriminator, plus an additive `investment_navs.basis` column (NULL ⇒ treated as `'reported'`, no backfill). No total-return column is persisted: the archetype total-return series is derived on read (ADR-0079 §3), keeping the analytics layer pure (ADR-0013). Migration `b017` historised the composition-weight tables to this same pattern (ADR-0080, above), and migration `b018` corrected the AnlV category catalogue to the statute (ADR-0083, see the Phase 7 block below). The archetype-aware analytics, the Excel import-format extension and sample data, and the Front-Office universe-chart triplet are covered by ADR-0081 and ADR-0082 respectively — see those ADRs rather than reproducing the detail here.

**Position model (ADR-0097, ADR-0098; migrations `b024`–`b025`).** A transaction-driven, unitised valuation path for listed instruments, landed July 2026. Migration `b024` added two tenant-scoped tables and one column. `position_transactions` is an append-style ledger of `txn_type ∈ {'opening', 'buy', 'sell', 'transfer'}` rows carrying `trade_date`, signed `units`, optional `price_per_unit` / `consideration`, and `ingest_origin`; a partial unique index admits at most one `opening` row per investment. `instrument_prices` is a per-unit price series keyed `(investment_id, as_of_date)` — distinct from `investment_navs`, which stores *position* values. `investments.valuation_mode ∈ {'reported', 'unitised'}` selects the write path per investment; the flip to `'unitised'` is a one-way operator act gated on the investment type and an anchoring `opening` transaction (ADR-0097 §6). **Holdings are never stored** — units held on a date are a pure derivation over the ledger (`services/investments/holdings.py`, ADR-0097 §4); the materialised computed-NAV rows are the persisted read product. Migration `b025` extended `investment_navs.ingest_origin` to `{'excel', 'live', 'manual', 'system'}`, where `'system'` marks a row written by the materialisation service — orthogonal to `basis` (`basis='computed'` says *how* the number was formed; `ingest_origin` says *who wrote it*), with book precedence `'excel'` > `'manual'` > `'system'`. Consumers of the NAV-series contract (analytics, charts, limits, Irene, reporting) are unchanged: computed NAVs arrive as ordinary `actual` rows. Synthetic unitisation of private-markets positions is specified in ADR-0097 §8 and deferred.

**Multi-currency (ADR-0099, ADR-0100, ADR-0101; migrations `b026`–`b027`).** The functional-currency model, landed July 2026. Migration `b026` added `tenants.functional_currency` (the portfolio's reporting currency, per tenant) and the tenant-scoped `fx_rates` table, whose rows are quoted against a **reference currency** — the base of the rate dataset, a property of the *data* rather than of the portfolio, and deliberately distinct from the functional currency even when the two coincide. `fx_rates` is RLS-protected through the standard `apply_tenant_rls(...)` policy like every other domain table. Rates are supplied by the Excel `FX rates` market-reference sheet in v1; a live FX-rate producer is roadmap #042. Migration `b027` added `'cash'` as the **eighth** `investment_type`, so a foreign-currency cash balance becomes a first-class `investments` row — an **explicit cash position** — converted, limit-checked and AnlV-classifiable through the machinery that already exists, rather than being folded into a residual that structurally cannot represent it (ADR-0100 §§1–2). Conversion happens at exactly **one boundary** (ADR-0099 §4): the data-assembly seam in front of the analytics layer, where `PortfolioReviewService` and `LimitsCoverageService` convert position-currency series into the functional currency via `services/fx/`. **ADR-0102 extended that boundary to the portfolio-analysis layer** — `PortfolioAnalysisService`, `StatisticsService`, and `BenchmarkComparisonService` now convert at the same seam, through the same `build_portfolio_fx_converter` idiom, so the frontier, the statistics KPIs, and the benchmark comparisons are measured in the functional currency (returns therefore include the FX effect, matching the FX-inclusive IRR/TVPI/DPI the review path already shipped) and no longer disagree with the Review header on a mixed-currency book. Decomposing that return into asset-performance and currency components is roadmap #045. Analytics therefore never sees a mixed-currency frame — it keeps the *stronger* single-currency contract it always had, and its ADR-0013 purity is untouched. A missing rate is a typed `MissingFxRateError`, never a silent 1:1; the affected section routes catch it and render an error partial with HTTP 200 (the ADR-0101 Overview / limits precedent). The ADR-0055 **cash residual** (`aum_total − Σ nav_functional`) was narrowed by ADR-0100 §3 to the functional-currency float and then **retired outright by ADR-0103 §2** (migration `b030` drops `portfolio_aum`): all cash — functional-currency included — is an explicit position, so AUM is defined uniformly as `Σ nav_functional(t)` over the whole book, with one shared formulation in `services/investments/aum.py` that the Overview hero, the coverage denominator and the `AUM`-sheet reconciliation control all resolve through. A denominator derived from the NAVs cannot go stale against them, which is what the residual's negative-suppression rule existed to paper over. On the surface, ADR-0101 adds a currency-exposure donut as the fourth Overview chart tile, an FX-cash card, and currency-aware money labels — all **conditionally rendered**, so a single-currency tenant's Overview is byte-for-byte what it was before the programme.

**Cases workflow (ADR-0107; migration `b031`).** The persistence layer for the eighth Area, landed July 2026. Three tenant-scoped tables land together: `cases` — a unit of decision work opened manually or from an Irene finding and closed exactly once with a mandatory `closing_note` (enforced in application code, not by a CHECK); `case_entries` — the append-only timeline, one row per situation, carrying a JSONB `payload` opaque to persistence that backs notes, decisions, and the three pin classes (documents, scenario snapshots, Shirley consultation excerpts); and `case_attachments` — in-database file bytes addressed only through their pin entry. All three carry a required `tenant_id` under the standard `apply_tenant_rls(...)` policy (ADR-0035); no audit triggers are installed (the `b019` idiom), and the `state` / `kind` / `actor` vocabularies are TEXT enforced in application code rather than SQL enums. The tables are wrapped by `case_repository` and `case_attachment_repository` in `core/repositories/`.

**Watchpoint registry and Watch Desk calibration (ADR-0116; migration `b033`).** Two tenant-scoped, **historised, audit-triggered** tables landed in August 2026, and with them the Watch Desk stopped being configured in code.

- `watchpoints` — what the desk observes and at which thresholds. A stable `watchpoint_id` with **immutable version rows** keyed `(tenant_id, watchpoint_id, effective_from)`, following the `limit_sets` pattern (ADR-0056): an edit inserts a new version, nothing is updated in place, and retirement is a version with `retired = true` so the identity and its history stay queryable and a past finding stays explainable. Seven families in one table, with **two shapes** enforced by per-family CHECKs. For the derived families (`saa`, `anlv`, `rss`) a watchpoint is a *sensitivity overlay only* — `muted`, `warn_threshold_pct`, `re_trigger_delta`, and for `rss` the mute alone — because the subject is enumerated from the effective limit sets or the curated tag vocabulary, and there is never a second edit point for a ceiling. For the four **defined** families (`price`, `fx`, `freshness`, `liquidity`) the watchpoint *defines* the subject and must carry its family's parameter columns. `freshness` and `liquidity` are **singletons**: at most one live identity per tenant, enforced in the repository, because their parameters apply to every investment or to the book as a whole.
- `floor_calibration` — the tenant's `FloorConfig` deviations, on the same versioned pattern. **An absent row means the code defaults**: nothing is seeded, so a later change to a platform default still reaches every tenant that never overrode it, and the editor always renders the *effective* value with a per-field default/customised marker. Composition (defaults ⊕ revision ⊕ per-subject overlays) re-runs the full `FloorConfig` validation on every read, so a historical row a later boundary edit would invalidate cannot silently produce an inverted configuration — and a revision that no longer composes fails the tenant's beat **loudly** rather than degrading to the defaults, because running a configuration nobody chose is worse than not running.

  Four **pinned invariants** are not tenant knobs under any framing, and the editor renders them locked with their rationale rather than merely refusing them on save: `fund_closure` floor = cap = 10 (a fixed level, not calibration — it has no column to be stored in); the `limit_breach` floor must sit inside the critical band, so a regulatory breach can never render below critical; and the caps on the RSS source and on `all_clear` stay at or below the informational boundary, so a standalone press cluster never outranks an internal finding and an all-clear is never itself urgent. Everything else — band cut points, the remaining floors and caps, the options gate, the WARN default, the per-family re-trigger deltas — is per-tenant calibration.

**The four signal families** (ADR-0116 §4) state their magnitude in **badness units** — a scalar where larger is always worse — so ADR-0087's direction-agnostic edge arithmetic is reused with no branch. Internally the statuses stay `OK`/`WARN`/`BREACH`; every surface renders them **Calm / Approaching / Triggered**, because "breach" is regulatory language reserved for the quota families. As implemented (the deviation is recorded on roadmap **#057**, not in the ADR — see below):

| Family | Subject | Magnitude, as implemented | Trigger |
|---|---|---|---|
| `price` | `price:{instrument_id}` | adverse (downward) move in pp over the window | move ≥ `drop_pct` |
| `fx` | `fx:{BASE}/{QUOTE}` | absolute move in pp over the window | \|move\| ≥ `move_pct` |
| `freshness` | `freshness:{investment_id}` | the **age** of the newest actual NAV in days, against `max_age_days` as the threshold | age ≥ `max_age_days` |
| `liquidity` | `liquidity:cash_coverage` | percent of the way down to the floor, `100 × min_coverage_ratio ÷ ratio`, on a fixed 100-point scale | magnitude ≥ 100 |

The last two depart from the ADR's magnitude table, and identically: the ADR's "excess beyond the threshold" phrasing is zero right up until the threshold is crossed, which leaves the WARN band unreachable and degrades the family to a binary. Measuring the quantity itself against the operator's own number restores Approaching on the same machinery the other two use. `liquidity`'s 100-scale is **arithmetic, not communication**: every human-facing string speaks in ratios (`1.08× against your 1.20× floor`), and the 100-scale never renders.

**One resolution, one observation, three call sites.** `effective_watchpoints` is *the* read, and the promise runs three layers deep: the beat and the monitor share one **resolution** (`services/watch_desk/overlay.py`), one **fetch** per family and one **producer** call (`services/watch_desk/signal_observation.py`, read-only). The monitor derives its rows live at request time from that shared path — never from `irene_watch_state`, whose acknowledged figures it reads only to explain a silence — so a row cannot disagree with the classification the next beat will make, and merely looking at the Watch Desk can never consume an edge. `tests/regression/test_watch_desk_single_resolution.py` pins all three layers structurally.

**Scoped settings (ADR-0112; migration `b032`).** One tenant-scoped `scoped_settings` table carrying per-field key-value rows for the application → tenant → user resolution chain, so a single setting can be defaulted globally, overridden per tenant, and overridden again per user. Its natural key is `UNIQUE NULLS NOT DISTINCT` — the scope columns are nullable at the broader levels, and the NULL-distinct default would have let duplicates through. The table carries the standard `apply_tenant_rls(...)` policy like every other domain table. Rows flagged `is_secret` are Fernet-encrypted through `services/credential_vault/`, whose master key (`CREDENTIAL_VAULT_MASTER_KEY`) comes from the environment and is never stored in the database. Written through `/admin#providers-credentials` and read through the `CredentialResolver` façade; see the Configuration section for the consumer detail.

**SAA domain (ADR-0042).** `saa_configurations` (per-tenant SAA scenarios), `saa_asset_class_inputs` (expected return + volatility per asset class per scenario), `saa_correlations` (pairwise correlation matrix, lower-triangular storage).

**Excel uploads.** `data_uploads` records every uploaded workbook with metadata + hash. `data_upload_sheets` records the per-sheet parse outcome. The Excel transformation pipeline lives in `services/data_normalization/`; `InvestmentExtractor` is its public entry point.

**Audit log.** `audit_log` is a generic append-only event log keyed by `(tenant_id, ts, kind, payload)`. Currently captures investment writes and SAA configuration writes; will expand as B1 lands.

**Phase 7 — Anlagegrenzen-Überwachung (ADRs 0055/0056/0057).** A coordinated set of three tables landed in 2026-05-19 with migration `b010`:

- `anlv_categories` — global stammtabelle of German Anlageverordnung classification codes (e.g. `§ 2 Abs. 1 Nr. 1 AnlV`), seeded from `services/data_normalization/fixtures/anlv_categories.json`. Investments link via the nullable `investments.anlv_code` FK (ADR-0057).
- `limit_sets` + `limits` — historised investment-limit catalogue. A `limit_set` is identified by `(family, effective_from)` with `family ∈ {'saa', 'anlv'}`; the row is immutable. Historical limit evaluations resolve via `effective_from` lookup so that past coverage stays reproducible across catalogue changes (ADR-0056).

The data layer, the coverage engine (`services/analytics/limit_coverage.py`), the Excel-import path, and the read-only web surface at `/back-office#limits` are all in production (roadmap B5 `mostly-done`). The editing surface — limit-set CRUD with CSRF/validation/audit — is the deferred remainder (roadmap B5d and follow-ups).

The full ORM model lives in `core/models/`; the repositories that wrap them in `core/repositories/`. Cross-aggregate orchestration lives in services like `services/investments/investment_service.py`, not in the repositories themselves.

---

## Cross-cutting concerns

### Theming

All visual parameters — chart colours, fonts, line widths, layout tokens, pill styles — live in JSON files under `config/`:

- `chart_theme.json` (default dark), `chart_theme_light.json`, `chart_theme_print.json` — ADR-0021.
- `ui_theme.json` (default dark), `ui_theme_corporate_blue.json`, `ui_theme_light.json` — ADR-0025, extended by ADR-0032.

`core/chart_theme.py` and `core/ui_theme.py` load and validate them. `core/theme_service.py` owns the active theme dict. The web variant materialises tokens into CSS variables via `scripts/generate_theme_artifacts.py` (writes `web/static/css/theme.css`); a pre-commit hook regenerates `theme.css` whenever a relevant JSON source changes.

No chart or widget code contains hardcoded colours, fonts, or line widths.

### Logging

`core/logging_setup.py` is the only place that configures logging. Called once per process from each entry point (`web/main.py`, `cli/__init__.py`, `bot/telegram_bot.py`). Logger hierarchies are nested by package: `portfoliflow.web`, `portfoliflow.cli`, `services.web_research`, etc. `print()` is forbidden outside CLI scripts.

### Configuration

All settings flow from `.env` (via `python-dotenv` and `pydantic-settings`) through `core/config.py` (`Settings`) and `web/settings.py` (`WebSettings`). The bot has its own `BotSettings` in `bot/config.py` so it can load without first importing `core.config` (which the bot regression-guard test forbids). Shirley's three runtime keys (`OPENROUTER_BASE_URL`, `OPENROUTER_API_KEY`, `SHIRLEY_MODEL`) are shared by web and bot.

Since ADR-0112 the environment is only the **application scope** of a three-scope chain (application → tenant → user). Tenant- and user-scope rows live in `scoped_settings`, secrets encrypted by `services/credential_vault/` under `CREDENTIAL_VAULT_MASTER_KEY`, and are written through `/admin#providers-credentials` (§6). `services/investments/credential_resolver.py` is the single façade that resolves the chain for consumers. Note which consumers are wired: the market-data path (OpenFIGI) resolves through it end to end, and so does OpenRouter — the chat route resolves per turn, the Irene tick per beat and per tenant, and the Telegram handler per turn (as the *paired user*, since ADR-0112 §5), each inside that tenant's context, so a row applies without a restart (ADR-0112 §4b). The Telegram **bot token** is read too, but at bot start rather than per turn, and by a cross-tenant scan rather than through the façade — a token change therefore applies at the next restart (ADR-0112 §5). One consumer still reads the application scope alone: the one-shot extraction path shared by the Report Scraper and the News Scraper's Fetcher-LLM, which runs on synchronous tool threads with no tenant context to resolve in.

### Excel import format

The multi-sheet Excel import format (ADR-0009) is the primary path
for investor onboarding and portfolio data refresh. Three structural
invariants:

- Column A is the label/date column; columns to its right are data columns.
- Row 1 has investment names, row 2 has types (investment sheets only), row 3 has sub-classes (investment sheets only); the parser discovers column counts dynamically from row 1.
- Three sheet categories: Attributes (key-value), investment time-series (date-indexed, cross-sheet column-consistent), market reference (date-indexed, independent namespace).

The format is designed so adding investments requires only adding columns to the Excel file — never changing the import code.

The persisted database identifier `format_version = "v2"` (in `data_uploads.format_version`) is preserved as an immutable historical handle (ADR-0059); the format is described in prose as the "Excel import format".

---

## The strangler migration (Phases 1–6)

PortfoliFLOW migrated from PyQt6 to FastAPI under a tagged-strangler pattern (ADR-0039) where the GUI and the web variant ran side-by-side on different ports, sharing the AI substrate but with no shared data path during transition (ADR-0041).

| Phase | Scope | Key ADRs |
|---|---|---|
| 1 | Foundations: layered architecture, registry, DataStore, exceptions, glossary, ADR system | ADR-0001 … ADR-0016 |
| 2 | AI substrate + theming: AIService, ToolRegistry, chart theming, UI theming, reporting Phase 1, Report Scraper, web research | ADR-0020 … ADR-0028 |
| 3 | Headless Shirley + Telegram bot + Strangler architecture decision: AIServiceCore Qt split, persistence backend, multi-tenancy via RLS, authentication, frontend stack | ADR-0029 … ADR-0041 |
| 4 | SAA domain + Investment domain + persistence entry points: schema migrations b005–b006, repository pattern wiring | ADR-0042 … ADR-0044 |
| 5 | Charts + Statistics + Portfolio Analysis web migration; analytics-service foundation under `services/analytics/`; sector/country weights schema | ADR-0045 |
| 6 (Block 1) | Frontend re-architecture: sidebar IA, area + section pattern, region model, Shirley embedded in Assistants, SAA lifted into Back Office section, AI Settings under Admin | ADR-0046 … ADR-0054 |
| 7 | Anlagegrenzen-Überwachung: AnlV-Klassifikation, AUM time-series, historised limit sets; data-layer landed 2026-05-19 (migration `b010`); coverage engine, import path, and read-only web surface (`/back-office#limits`) delivered, editing surface deferred | ADR-0055 … ADR-0057, ADR-0060 |
| Post-7 (roadmap-driven) | Shipped work after the strangler phases, tracked by flat roadmap IDs rather than a phase number: Benchmarks & Attribution (migration `b011`), multi-tenant activation & super-admin (`b012`–`b014`), the Front-Office welcome header / `users.display_name` (`b015`), cash-flow-adjusted frontier, Front-Office Overview (KPI strip + chart row), single-investment reviews, per-tenant seed parity & RLS-in-`tenant_context`, multimodal & voice I/O for Shirley, and the liquid-asset archetypes (`b016`–`b018`) | ADR-0061, ADR-0063/0064, ADR-0068, ADR-0066, ADR-0067/0072, ADR-0073, ADR-0075/0076, ADR-0077/0078, ADR-0079 … ADR-0083 |

Phase 6 Block 2 (Multi-User & Permissions), Block 3 (GUI Sunset), and Block 4 (Consolidation) were planned in the Mission Control pattern but reorganised into roadmap items B1, B2, and the quick-win items after Block 1 completed. The Mission Control chat pattern is retired (see `docs/roadmap.md` introduction).

The web variant tag-pattern: each phase ends with a `demo-stable-*` git tag so the GUI and the web variant can be co-deployed at a known good state during the migration window.

The migration closed with the **Qt sunset** (ADR-0094 Stage 1, July 2026): the `gui/` tree, the `main.py` desktop entry point, the Qt AIService adapter and its `services/ai_service.py` deprecation shim, and the PyQt6 (and Qt test) dependencies were removed, and the former top-level `analytics/` package was folded into `services/analytics/`. The pre-removal state is preserved at the `demo-stable-pre-qt-sunset` git tag. The in-memory DataStore complex the GUI left behind is deferred to a Stage-2 decommission (ADR-0094 §5, roadmap #035).

---

## Planned architecture (not yet implemented)

The following decisions are made or scoped but not yet built. **Do not implement them unsolicited.** If a prompt asks for one, read the linked ADR or roadmap item first and confirm scope.

| Topic | Source |
|---|---|
| Multi-User & Permissions (owner/member model, per-user sessions, tool-trust per-role overlay) | Roadmap B1 |
| DataStore complex decommission (Qt-sunset Stage 2) | ADR-0094 §5; roadmap #035 |
| Hetzner Deployment (production-grade web hosting) | Roadmap C1 |
| Portfolio Review PDF export | Roadmap A1 |
| Anlagegrenzen limit-set editing surface (CRUD; data layer, engine, import, read-only surface already shipped) | Roadmap #019 follow-ups |
| Shirley code-generation & sandbox execution | Roadmap B3 |
| Shirley full app control via per-module tools | Roadmap B4 |
| Reporting engine Phase 2 (PDF/PPTX file export) | ADR-0020 |
| UI theme schema extension (`layout` / `pill` / extended `font`) | ADR-0032 |
| DataVault (legacy planning name; replaced by Postgres per ADR-0034) | ADR-0017 (superseded) |

Carry-forward rules that already apply today even though the corresponding features are not yet built:

- **Multi-user code is not to be written now**, but no current code may make assumptions that preclude a future 3–8 user team with RBAC. The audit substrate is already active; new tables must follow the audit-column convention.
- **Business logic must not live inside presentation classes.** Every function that computes, queries, or transforms data must be callable in a unit test without starting the FastAPI app. The heuristic: *"Could I call this in a unit test without instantiating a TestClient?"* If no, it belongs in a service.
- **The term *scraper* is reserved for two distinct things.** The **News Scraper** (`services/web_research/`, RSS press coverage for Shirley) and the **Report Scraper** (`modules/assistants/report_scraper.py` + `services/scraper/`, GP quarterly report extraction). Use the qualified term in docs and commit messages; never the bare word.

---

## What does *not* belong in this codebase

- **Hardcoded colours, fonts, line widths.** Theming is centralised; chart code reads from the active theme dict.
- **Bare `print()` outside CLI scripts.** Use logging.
- **`from PyQt6 import …` anywhere.** PyQt6 was removed with the Qt surface (ADR-0094 Stage 1); no allowance remains. The bot's `test_no_qt_import` guard (`tests/bot/test_telegram_bot.py`, ADR-0030) continues to enforce it on the bot path.
- **`get_data_store()` or `PersistentDataStore` imports inside `web/`.** ADR-0041 — the web side goes through repositories. A regression test (`tests/regression/test_web_does_not_import_persistent_data_store.py`) enforces this.
- **`matplotlib` inside `web/`.** Charts in the web variant are Plotly specs. A regression test (`tests/regression/test_no_matplotlib_in_web.py`) enforces this.
- **Module-to-module imports.** A module never imports from a sibling module — shared logic lives in `core/`, `services/`, or `services/analytics/`. (The former top-level `analytics/` package was folded into `services/analytics/` by ADR-0094.)
- **Lazy imports to break a cycle.** A circular import is a design error.
- **`setup.py`.** This project uses `pyproject.toml` (PEP 621) exclusively.
- **Raw `Exception` raises or bare `except Exception:` blocks.** Every project exception subclasses `PortfoliFlowError`.

---

## Architecture review protocol

When a substantial architectural change is on the table — a new persistence layer, a refactor of the service split, the introduction of a new top-level layer — the workflow is:

1. Generate a Repomix snapshot.
2. Submit it to Claude Opus with a focused review prompt. Example for a Repository-pattern audit:
   > "Review this PortfoliFLOW codebase with focus on Separation of Concerns and Repository-pattern readiness. Identify all locations where business logic is embedded directly in PyQt6 classes or in route handlers and would need to be extracted into a Service Layer before a future schema migration can be cleanly integrated. Prioritise findings by refactoring effort."
3. Capture the resulting decision in a new ADR before any code is written.
4. The ADR is the prompt basis for the implementation chat (typically Sonnet/Claude Code).

This protocol is documented in ADR-0015 as the Claude-assisted development workflow.

---

## How to read the ADR index

The ADR index lives in `docs/adr/README.md`. Numbered chronologically, ADR-0001 onward, with an explicit thematic tag (`architecture`, `process`, `data`, `integration`, `ui`, `security`, `web-migration`, `multi-tenant`, etc.) and a status:

- **Proposed** — drafted, not yet decided.
- **Accepted** — in effect. The codebase reflects it.
- **Deprecated** — no longer recommended; new code should not rely on it.
- **Superseded by ADR-XXXX** — replaced by a later decision. The old ADR stays in the repo unchanged for historical traceability.

ADRs are immutable in spirit. When a decision changes, write a new ADR rather than editing the old one. Status transitions are recorded in the revision history table at the bottom of each ADR. The decision *log* is the deliverable — not just the current snapshot.

Where an ADR has been renumbered for housekeeping (e.g. ADR-0058
was renumbered from a draft 0046 to resolve a number collision —
see ADR-0058's revision history), the new file is the canonical
reference and the old slot is left unused. Such renumberings are
recorded in the affected ADR's revision history.

If something in this document and an ADR disagree, the ADR is authoritative; this document is overdue for an update.
