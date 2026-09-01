# Architecture Decision Records (ADRs)

This directory contains the **Architecture Decision Records** for PortfoliFLOW. An ADR is a short document that captures a single architecturally significant decision, its context, and its consequences. Taken together, the ADRs form a chronological log of *why* PortfoliFLOW is built the way it is.

ADRs exist to make decisions traceable — for the development team, for future maintainers, and for external reviewers (auditors, compliance, institutional investors, GP partners). They complement, but do not replace, code-level documentation and architecture documentation (e.g., arc42).

## Why We Keep ADRs

PortfoliFLOW is built for institutional use cases where code must be auditable by humans and machines. Code and docstrings tell *what* the system does; ADRs tell *why* it was designed that way, and *what was considered but rejected*. In an audit context (BAIT/VAIT, DORA, SOC 2, ISO 25010), this kind of traceability is not optional — it is the evidence that decisions were made deliberately rather than by accident.

## Format

All ADRs follow the template in [`template.md`](./template.md). The format is adapted from Michael Nygard's original proposal and extended with sections relevant to the institutional and regulatory context PortfoliFLOW operates in.

Each ADR is a single Markdown file. ADRs are immutable in spirit: once accepted, they are not rewritten. When a decision changes, a new ADR supersedes the old one and the old one is marked accordingly (see *Lifecycle* below).

## Naming and Numbering

- File name pattern: `NNNN-short-kebab-case-title.md`
- `NNNN` is a zero-padded four-digit sequence number, starting at `0001`.
- Numbers are never reused. Superseded ADRs keep their number.
- The title in the file name should match the title in the ADR heading.

Examples:

- `0001-layer-separation-business-logic-vs-pyqt.md`
- `0002-datastore-as-singleton.md`
- `0011-aiservice-singleton-with-openrouter-router.md`

## Lifecycle (Status Field)

Every ADR has exactly one status at any given time:

- **Proposed** — drafted but not yet decided. Open for discussion.
- **Accepted** — the decision is in effect. The codebase should reflect it.
- **Deprecated** — the decision is no longer recommended, but has not been formally replaced. New code should avoid relying on it.
- **Superseded by ADR-XXXX** — the decision has been replaced by another ADR. The old ADR stays in the repository unchanged for historical traceability.

Status transitions are recorded in the *Revision History* table at the bottom of each ADR.

## When to Write an ADR

Write an ADR when a decision meets at least one of these criteria:

1. It affects the architecture of the system (module boundaries, layering, data flow, persistence strategy, concurrency model, integration points).
2. It establishes a convention that others must follow (naming, documentation style, commit format, language choice).
3. It has compliance or audit relevance (security, access control, data handling, reproducibility of calculations, logging, change management).
4. It locks in a significant external dependency (framework, library, service, LLM provider).
5. It is the kind of decision that, if reversed later, would require non-trivial rework.

Day-to-day implementation choices (which helper function to extract, which variable name to use) do not need an ADR. When in doubt, err toward writing one — short and imperfect is better than missing.

## How to Propose an ADR

1. Copy `template.md` to `NNNN-your-title.md` with the next free number.
2. Fill in the sections. Leave the status at `Proposed`.
3. Commit the file on a branch and open a pull request (or mark it as a discussion point in the next review, depending on team workflow).
4. Once the decision is made, update the status to `Accepted` and merge.
5. If a later ADR replaces this one, update the status to `Superseded by ADR-XXXX` and link the new ADR in *References*.

## Index

A current index of ADRs can be generated with a short script or maintained manually in this README. Suggested columns: number, title, status, date, tags. The index is not authoritative — the individual ADR files are.

| #    | Title | Status | Date | Tags |
|------|-------|--------|------|------|
| 0000 | [Retrofit Report](./0000-retrofit-report.md) | Informational | 2026-04-24 | process, meta |
| 0001 | [Layered Architecture and Strict One-Way Dependencies](./0001-layered-architecture-and-strict-one-way-dependencies.md) | Accepted | 2026-04-24 | architecture |
| 0002 | [Canonical Glossary — Area, Module, Feature, Function, Widget, Panel, Service](./0002-canonical-glossary.md) | Superseded by ADR-0084 | 2026-04-24 | process, architecture |
| 0003 | [BaseModule Contract and ModuleRegistry as Single Seam](./0003-basemodule-contract-and-module-registry-as-single-seam.md) | Accepted | 2026-04-24 | architecture |
| 0004 | [In-Memory DataStore Singleton with Documented Extension Path](./0004-in-memory-datastore-singleton.md) | Accepted | 2026-04-24 | architecture, data |
| 0005 | [Typed Exception Hierarchy Rooted in PortfoliFlowError](./0005-typed-exception-hierarchy.md) | Superseded by ADR-0044 | 2026-04-24 | architecture, process |
| 0006 | [Python 3.11+ with Modern Type Syntax and Mandatory Type Hints](./0006-python-3-11-and-modern-type-syntax.md) | Accepted | 2026-04-24 | process |
| 0007 | [Google-Style Docstrings on All Public APIs](./0007-google-style-docstrings.md) | Accepted | 2026-04-24 | process |
| 0008 | [English as the Sole Codebase Language](./0008-english-as-the-sole-codebase-language.md) | Accepted | 2026-04-24 | process |
| 0009 | [Excel V2 Multi-Sheet Import Format with Dynamic Column Discovery](./0009-excel-v2-import-format.md) | Accepted | 2026-04-24 | data, integration |
| 0010 | [AIService as Singleton, OpenAI-Compatible Endpoints](./0010-aiservice-singleton-openai-compatible-endpoints.md) | Accepted | 2026-04-24 | integration, architecture |
| 0011 | [Acknowledged PyQt6 Dependency in AIService for Signals and Threads](./0011-pyqt6-dependency-in-aiservice.md) | Superseded by ADR-0094 | 2026-04-24 | architecture, integration, ui |
| 0012 | [ToolRegistry as Single Seam for AI-Callable Tools](./0012-toolregistry-as-single-seam.md) | Accepted | 2026-04-24 | integration, architecture |
| 0013 | [Analytics Layer — Pure, Stateless, No GUI or DataStore Dependencies](./0013-analytics-layer-pure-and-stateless.md) | Accepted | 2026-04-24 | architecture, analytics |
| 0014 | [Conventional Commits and Checkpoint-Commit Discipline Before AI Sessions](./0014-conventional-commits-and-checkpoint-discipline.md) | Accepted | 2026-04-24 | process |
| 0015 | [Claude-Assisted Development Workflow with Repomix and Model-Tier Split](./0015-claude-assisted-development-workflow.md) | Accepted | 2026-04-24 | process |
| 0016 | [Module-Scope Rule — Adding a Module Touches at Most Three Existing Lines](./0016-module-scope-rule-three-line-budget.md) | Accepted | 2026-04-24 | process, architecture |
| 0017 | [Planned DataVault — DuckDB-Backed Persistent Layer with Audit Fields](./0017-planned-datavault-duckdb.md) | Superseded by ADR-0034 | 2026-04-24 | data, architecture |
| 0018 | [Planned Service / Repository Layering as Prerequisite for Client-Server Migration](./0018-planned-service-repository-layering.md) | Accepted (initial implementation in Strang B of Phase 1) | 2026-04-24 | architecture, process |
| 0019 | [Planned Multi-User Readiness via Audit Fields, No Multi-User Code Yet](./0019-planned-multi-user-readiness.md) | Accepted | 2026-04-24 | architecture, security |
| 0020 | [Planned Reporting Engine — Three-Layer Design (Data / Template / Style)](./0020-planned-reporting-engine-three-layer.md) | Proposed | 2026-04-24 | architecture, ui, integration |
| 0021 | [Chart Theming Externalised to a Single JSON Config](./0021-chart-theming-externalised-to-json.md) | Accepted | 2026-04-24 | ui, process |
| 0022 | [Tool Trust Classes and Gating Policy](./0022-tool-trust-classes-and-gating-policy.md) | Accepted | 2026-04-24 | security, integration, architecture |
| 0023 | [Web Research Capability (Architecture)](./0023-web-research-capability.md) | Accepted | 2026-04-24 | security, integration, architecture |
| 0024 | [RSS-based Source Resolution for Web Research](./0024-rss-based-source-resolution.md) | Accepted | 2026-04-24 | security, integration, architecture |
| 0025 | [UI Theming System with Multi-Variant Support](./0025-ui-theming-system-with-multi-variant-support.md) | Accepted | 2026-04-27 | ui, process, architecture |
| 0026 | [Phase-1 Reporting Engine — In-App Multi-Tile Rendering](./0026-phase-1-reporting-engine-in-app-multi-tile-rendering.md) | Accepted | 2026-04-27 | architecture, ui, integration, analytics |
| 0027 | [Report Scraper Implementation](./0027-report-scraper-implementation.md) | Accepted | 2026-04-27 | integration, architecture, data, ui |
| 0028 | [`generate_chart` Tool as `READ_INTERNAL` — Member Extension to ADR-0012](./0028-generate-chart-tool-as-read-internal.md) | Accepted | 2026-04-27 | integration, security, ui |
| 0029 | [Headless Shirley as Qt-Free Synchronous Entry Point for Non-GUI Clients](./0029-headless-shirley-qt-free-entry-point.md) | Superseded by ADR-0038 | 2026-04-29 | architecture, integration |
| 0030 | [Telegram Bot as First Non-GUI Client of Headless Shirley](./0030-telegram-bot-as-first-headless-client.md) | Accepted | 2026-04-29 | integration, architecture, security |
| 0031 | [Module-Level Threading Lock as Interim Concurrency Control for Bot-Side Turns](./0031-module-level-threading-lock-interim-concurrency.md) | Accepted | 2026-04-29 | architecture, security, integration |
| 0032 | [UI Theme Schema Extension for Layout, Pill, and Font Tokens](./0032-ui-theme-schema-extension-layout-pill-font.md) | Proposed | 2026-04-29 | ui, architecture, process |
| 0033 | [Web Migration — Architectural Shift from PyQt6 Desktop to FastAPI Web](./0033-web-migration-pyqt6-desktop-to-fastapi-web.md) | Accepted | 2026-05-03 | web-migration, architecture, process, integration |
| 0034 | [Persistence Backend — Postgres for Multi-Tenant Operation](./0034-persistence-backend-postgres-for-multi-tenant-operation.md) | Accepted (supersedes ADR-0017) | 2026-05-03 | web-migration, persistence, postgres, multi-tenant |
| 0035 | [Multi-Tenant Architecture — Tenant Isolation via tenant_id and Row-Level Security](./0035-multi-tenant-architecture-tenant-isolation-via-rls.md) | Accepted | 2026-05-03 | web-migration, multi-tenant, security, data-isolation |
| 0036 | [Authentication Strategy — Session-Based with OIDC-Readiness](./0036-authentication-strategy-session-based-with-oidc-readiness.md) | Accepted | 2026-05-03 | web-migration, authentication, security, session-management |
| 0037 | [Frontend Stack — FastAPI + Jinja + HTMX, Server-Side Rendering as Default](./0037-frontend-stack-fastapi-jinja-htmx-ssr-default.md) | Accepted | 2026-05-03 | web-migration, frontend, htmx, server-side-rendering |
| 0038 | [AIService Refactoring — Qt-Free Core with Qt Adapter](./0038-ai-service-refactoring-qt-free-core-with-qt-adapter.md) | Accepted | 2026-05-03 | web-migration, ai-service, refactoring, qt-decoupling |
| 0039 | [Migration Pattern — Strangler with Tagged Demo-Stable Branch](./0039-migration-pattern-strangler-with-tagged-demo-stable-branch.md) | Accepted | 2026-05-03 | web-migration, process, architecture |
| 0040 | [Sentinel Bootstrap — CLI-Driven Idempotent Initialization](./0040-sentinel-bootstrap-cli-driven.md) | Accepted | 2026-05-04 | web-migration, bootstrap, cli, deployment, multi-tenant |
| 0041 | [Persistence Entry-Points — Strangler-Coexistence of In-Memory and Postgres](./0041-persistence-entry-points-strangler-coexistence.md) | Accepted | 2026-05-04 | web-migration, persistence, strangler, architecture |
| 0042 | [Phase 3 Scope — SAA-Only Domain Schema and Plotly-First Charting](./0042-phase-3-scope-saa-only-and-charting-architecture.md) | Accepted | 2026-05-05 | web-migration, domain-schema, charting, scope, architecture |
| 0043 | [Investment Domain Schema and Excel Transformation Pathway](./0043-investment-domain-schema-and-excel-transformation.md) | Accepted | 2026-05-06 | web-migration, domain-schema, persistence, excel-import, architecture |
| 0044 | [Rename PortfolioFlowError to PortfoliFlowError for Project-Name Unification](./0044-rename-portfolioflowerror-to-portfoliflowerror.md) | Accepted | 2026-05-06 | process, architecture |
| 0045 | [Charts/Statistics Web Migration and Analytics-Service Foundation](./0045-charts-statistics-web-migration-and-analytics-service-foundation.md) | Accepted | 2026-05-07 | web-migration, charts, statistics, analytics, plotly, sector-country, schema, phase-5 |
| 0046 | [Region Model for Country Aggregation](./0046-region-model-for-country-aggregation.md) | Accepted | 2026-05-12 | schema, regions, countries, excel-import, portfolio-review, phase-6, anti-debt |
| 0047 | [Tool-Execution Context Propagation — Tenant + Engine Seam for Postgres-Native AI Tools](./0047-tool-execution-context-propagation.md) | Accepted | 2026-05-14 | web-migration, ai-service, multi-tenant, persistence, architecture, strangler |
| 0048 | [Two-Axis Chart Architecture for Shirley — Semantic Data Tools + Generic Plotly Renderer](./0048-shirley-two-axis-chart-architecture.md) | Accepted | 2026-05-14 | architecture, integration, ui, analytics |
| 0049 | [Shirley Tool-Orchestration Guidance in a Runtime-Appended Context File](./0049-shirley-tool-orchestration-in-runtime-context.md) | Accepted | 2026-05-15 | integration, ui, process |
| 0050 | [Multi-Turn Chat History — In-Memory, Per-Session, Bounded](./0050-multi-turn-chat-history-in-memory.md) | Accepted | 2026-05-15 | architecture, integration, ui |
| 0051 | [Shirley Embedded in the Assistants Area; `/chat` Retired](./0051-shirley-embedded-in-assistants-area.md) | Accepted | 2026-05-15 | architecture, integration, ui |
| 0052 | [AI Settings — Runtime-Editable Under `/admin`, Persistence Deferred](./0052-ai-settings-runtime-under-admin.md) | Accepted | 2026-05-15 | architecture, integration, ui, configuration |
| 0053 | [Report Scraper Web Surface under `/assistants#report-scraper`](./0053-report-scraper-web-surface.md) | Accepted | 2026-05-15 | architecture, integration, ui |
| 0054 | [SAA Surface Consolidation into Back-Office Section](./0054-saa-surface-consolidation-into-back-office-section.md) | Accepted | 2026-05-15 | architecture, ui, integration |
| 0055 | [Cash as Residual in AUM Coverage Engine](./0055-cash-as-residual-in-aum-coverage.md) | Accepted | 2026-05-19 | schema, limits, aum, cash, engine-contract, anlagegrenzen, phase-7 |
| 0056 | [Limit-Set Historisierung via `effective_from`](./0056-limit-set-historization-via-effective-from.md) | Accepted | 2026-05-19 | schema, limits, historization, immutability, anlagegrenzen, phase-7 |
| 0057 | [AnlV Classification as 1:1 Investment Attribute](./0057-anlv-classification-as-1to1-investment-attribute.md) | Accepted | 2026-05-19 | schema, anlv, investments, classification, regulatory, anlagegrenzen, phase-7 |
| 0058 | [Web Information Architecture — Sidebar Plus Long-Scroll Areas](./0058-web-information-architecture.md) | Accepted | 2026-05-10 | frontend, ui, web, htmx, ia, phase-6 |
| 0059 | [Excel Import Format — Naming Hygiene](./0059-excel-import-format-naming.md) | Accepted | 2026-05-20 | process, naming, excel-import, language-hygiene |
| 0060 | [NAV Carry-Forward with Cross-Stream Fallback in the Limit-Coverage Engine](./0060-nav-carry-forward-and-cross-stream-fallback.md) | Accepted | 2026-05-21 | engine-contract, limits, nav, anlagegrenzen, phase-7 |
| 0061 | [Benchmarks & Attribution — Schema, Import, and Analytics Architecture](./0061-benchmarks-and-attribution-schema-import-and-analytics.md) | Accepted | 2026-05-24 | schema, benchmarks, attribution, excel-import, back-office, analytics, phase-7 |
| 0062 | [Visual Conventions for Tables and Charts (Program-Wide)](./0062-visual-conventions-for-tables-and-charts.md) | Accepted | 2026-05-26 | ui, theming, charts, tables, cross-surface, design-system, phase-1b |
| 0063 | [Multi-Tenant Activation (Phase 1) — Subdomain Routing and Role Model](./0063-multi-tenant-activation-phase-1-subdomain-routing-and-role-model.md) | Accepted | 2026-05-26 | web-migration, multi-tenant, authentication, security, authorisation, role-model |
| 0064 | [Super-Admin Surface — CLI-Driven Platform Operations, No Web-Side Tenant-Data Access](./0064-super-admin-surface-cli-driven-platform-operations.md) | Accepted | 2026-05-26 | multi-tenant, super-admin, platform-operations, security, audit, cli |
| 0065 | [Request-Scoped Transaction Lifetime and Session-Touch Placement](./0065-request-scoped-transaction-lifetime-and-session-touch.md) | Accepted | 2026-05-28 | web-migration, database, concurrency, authentication, session-management, performance, multi-tenant |
| 0066 | [Cash-Flow-Adjusted Returns for the Portfolio Analysis Frontier](./0066-cashflow-adjusted-frontier-returns.md) | Accepted | 2026-05-28 | analytics, portfolio-analysis, frontier, returns, cashflow-adjusted |
| 0067 | [Front Office "Overview" — Portfolio Headline KPI Strip](./0067-front-office-overview-kpi-strip.md) | Accepted | 2026-05-29 | frontend, ui, web, htmx, front-office, aum, kpi, overview, phase-6 |
| 0068 | [Front Office Welcome Header and `users.display_name`](./0068-front-office-welcome-header-and-user-display-name.md) | Accepted | 2026-05-29 | frontend, ui, web, front-office, greeting, schema, users, auth, theming, phase-6 |
| 0069 | [Back-Office Analysis Tools for Shirley — Limit Coverage, SAA-Hypothetical, Portfolio Statistics as `READ_INTERNAL` Tools](./0069-shirley-back-office-analysis-tools.md) | Accepted | 2026-06-01 | ai-service, tools, shirley, read-internal, phase-6, phase-7 |
| 0070 | [Shirley Analysis Read Tools — Phase 2 (Deterministic Surfaces)](./0070-shirley-analysis-read-tools-phase-2.md) | Proposed (stub) | 2026-06-01 | ai-service, tools, shirley, read-internal |
| 0071 | [Persistent Analysis-Results Store — Run-Bound Results for Shirley](./0071-persistent-analysis-results-store.md) | Proposed (stub) | 2026-06-01 | persistence, schema, scraper, shirley, tools, trust |
| 0072 | [Front Office "Overview" — Chart Row and Fund-Composition Pareto](./0072-front-office-overview-chart-row-and-fund-composition-pareto.md) | Accepted | 2026-06-03 | frontend, ui, web, htmx, front-office, overview, charts |
| 0073 | [Single-Investment Reviews as a Per-Investment Lazy-Loaded Stack in the Portfolio Review Section](./0073-single-investment-review-web-surface.md) | Accepted | 2026-06-01 | web-migration, portfolio-review, investor-communication, htmx, charts, plotly |
| 0074 | [Product Scope — Institutional Portfolio Management Platform](./0074-product-scope-institutional-portfolio-management.md) | Accepted | 2026-06-03 | product-scope, positioning, documentation, governance |
| 0075 | [Multimodal Image Input for Shirley — Vision on the Web and Telegram Surfaces](./0075-multimodal-image-input-for-shirley.md) | Accepted | 2026-06-04 | ai-service, shirley, web-migration, telegram, multimodal, demo |
| 0076 | [Voice I/O for Shirley — Speech-to-Text and Text-to-Speech on the Web and Telegram Surfaces](./0076-voice-io-for-shirley-stt-and-tts-on-web-and-telegram.md) | Accepted | 2026-06-04 | ai-service, shirley, voice, stt, tts, web, telegram, multimodal, demo |
| 0077 | [Per-Tenant Default-Seed Parity Between `bootstrap` and `create-tenant`](./0077-per-tenant-default-seed-parity.md) | Accepted | 2026-06-05 | tenant-provisioning, seeding, data-import, bootstrap-parity, governance |
| 0078 | [Enforce RLS in `tenant_context` via Application-Role Switch Under Privileged Connections](./0078-enforce-rls-in-tenant-context.md) | Accepted | 2026-06-05 | multi-tenancy, rls, tenant-isolation, provisioning, security, governance |
| 0079 | [Liquid-Asset Archetypes, Per-Investment Schema, and Mark-to-Market Return Conventions](./0079-liquid-asset-archetypes-schema-and-return-conventions.md) | Accepted | 2026-06-15 | schema, liquid-archetypes, fixed-income, listed-equity, analytics, returns, time-series, per-investment |
| 0080 | [Historise the Composition-Weight Tables (sector / region / country)](./0080-historise-composition-weight-tables.md) | Accepted | 2026-06-15 | persistence, schema-migration, time-series, composition-weights, historisation, analytics, multi-tenancy |
| 0081 | [Liquid-Archetype Import-Format Extension and Sample-Data Coverage](./0081-liquid-archetype-import-format-and-sample-data.md) | Accepted | 2026-06-16 | schema, excel-import, liquid-archetypes, fixed-income, sample-data, data-import |
| 0082 | [Archetype-Aware Front-Office Universe-Charts Triplet](./0082-archetype-aware-front-office-universe-charts-triplet.md) | Accepted | 2026-06-16 | front-office, charts, chart-specs, liquid-archetypes, ui |
| 0083 | [Correct the AnlV Category Catalogue to the § 2 Abs. 1 AnlV Statute](./0083-correct-anlv-category-taxonomy-to-statute.md) | Accepted | 2026-06-16 | schema, anlv, regulatory, correction, data-fix |
| 0084 | [Glossary v2 — Section and Repository as First-Class Terms; Widget/Panel Demoted to Legacy Qt](./0084-glossary-v2-section-repository-legacy-qt.md) | Accepted | 2026-07-01 | process, architecture, glossary, web-migration |
| 0085 | [Irene Persistence Layer](./0085-irene-persistence-layer.md) | Accepted | 2026-07-02 | irene, decision-console, persistence, schema, multi-tenancy, rls, audit |
| 0086 | [Irene Cadence and Tick Adapter](./0086-irene-cadence-and-tick-adapter.md) | Accepted | 2026-07-02 | irene, decision-console, cadence, scheduling, cli, synthesis, concurrency |
| 0087 | [Irene Delta Mechanics](./0087-irene-delta-mechanics.md) | Accepted | 2026-07-02 | irene, decision-console, delta, materiality, rss, embeddings, determinism |
| 0088 | [Irene Synthesis Contract](./0088-irene-synthesis-contract.md) | Accepted | 2026-07-02 | irene, decision-console, synthesis, function-calling, urgency, floor-config, analytics-purity |
| 0089 | [Decision Console — Briefing UI and Action Model](./0089-decision-console-briefing-ui.md) | Accepted | 2026-07-02 | irene, decision-console, ui, htmx, briefing, journal, watchlist, action-model |
| 0090 | [Investment Security Identifiers and FIGI Normalisation](./0090-investment-security-identifiers-and-figi-normalisation.md) | Accepted | 2026-07-02 | investments, schema, data-import, market-data, multi-tenancy, identifiers |
| 0091 | [Market-Data Provider Port, Normalised DTO, and Adapter Architecture](./0091-market-data-provider-port-and-adapter-architecture.md) | Accepted | 2026-07-02 | market-data, data-import, ports-and-adapters, dto-contract, concurrency, architecture |
| 0092 | [Live-Ingest Contract and Excel Precedence](./0092-live-ingest-contract-and-excel-precedence.md) | Accepted | 2026-07-02 | market-data, data-import, schema, data-integrity, provenance, audit |
| 0093 | [Live-Import Trigger and Out-of-Process Tick Adapter](./0093-live-import-trigger-and-out-of-process-tick-adapter.md) | Accepted | 2026-07-02 | market-data, data-import, scheduling, multi-tenancy, concurrency, audit |
| 0094 | [GUI Sunset Execution — Remove the PyQt6 Surface, Fold Legacy Analytics, Retire Scaffold Modules](./0094-gui-sunset-execution.md) | Accepted (Stage 1; §5 Stage 2 open — roadmap #035) | 2026-07-02 | architecture, web-migration, legacy, sunset, dependencies, process |
| 0095 | [Provider Credential Vault — Per-Tenant Market-Data Credentials with Staged Adoption](./0095-provider-credential-vault.md) | Accepted | 2026-07-07 | market-data, security, multi-tenancy, configuration, compliance, audit |
| 0096 | [Identifier Scheme-Set Extension — Provider-Native Fund Identifiers and Human-Confirmed Mapping](./0096-identifier-scheme-set-extension.md) | Accepted | 2026-07-07 | market-data, data-import, identifiers, private-markets, schema, key-forming, audit |
| 0097 | [Position Model — Transaction Ledger, Holdings Derivation, Valuation Modes, and Instrument Prices](./0097-position-model-transactions-holdings-valuation-modes.md) | Accepted | 2026-07-08 | schema, position-model, unitised-valuation, transactions, market-data, rls, audit |
| 0098 | [Computed-NAV Materialisation and Live-Ingest Write-Path Re-Routing](./0098-computed-nav-materialisation-and-write-path-rerouting.md) | Accepted | 2026-07-08 | market-data, ingest, materialisation, nav, excel-precedence, regression |
| 0099 | [Multi-Currency Model — Functional Currency, FX Rates, and the Conversion Boundary](./0099-multi-currency-model-functional-currency-fx-rates-conversion-boundary.md) | Accepted | 2026-07-10 | schema, analytics, fx, currency, import, market-data, engine-contract, phase-8 |
| 0100 | [Explicit Foreign-Currency Cash Positions and the Redefined Cash Residual](./0100-explicit-foreign-currency-cash-positions-and-redefined-residual.md) | Accepted | 2026-07-10 | schema, cash, fx, currency, limits, aum, engine-contract, phase-8 |
| 0101 | [FX Exposure and Cash Visibility on the Front-Office Overview](./0101-fx-exposure-and-cash-visibility-front-office-overview.md) | Accepted | 2026-07-11 | front-office, overview, charts, fx, currency, ui, phase-8 |
| 0102 | [Statistics, SAA, and Benchmark Currency Contract — Extending the Conversion Boundary to the Portfolio-Analysis Layer](./0102-statistics-saa-currency-contract.md) | Accepted | 2026-07-11 | analytics, fx, currency, statistics, saa, benchmark, engine-contract, phase-8 |
| 0103 | [Cash as a First-Class Asset Class — Unitised Representation, Investor Flows, Plan Path, and Residual Retirement](./0103-cash-as-first-class-asset-class.md) | Accepted | 2026-07-13 | schema, cash, fx, currency, aum, limits, planning-desk, engine-contract, workbook, phase-8 |
| 0104 | [Plan-Path Materialisation and Scenario Overlay Architecture — the Planning Desk](./0104-plan-path-materialisation-and-scenario-overlay-architecture.md) | Accepted | 2026-07-13 | planning-desk, scenario, overlay, plan-path, fx, htmx, module-registry, engine-contract, phase-8 |
| 0105 | [Takahashi–Alexander Pacing Profiles — Ephemeral Generation for Plan-less Capital-Account Funds](./0105-takahashi-alexander-pacing-profiles.md) | Accepted | 2026-07-14 | planning-desk, pacing, takahashi-alexander, capital-account, plan-world, pure-engine, phase-8 |
| 0106 | [Decision Console `options` Rendered as Decision-Support Prose](./0106-decision-console-options-as-decision-support-prose.md) | Accepted | 2026-07-20 | decision-console, irene, synthesis, options, presentation, prompt-contract, analytics-purity |
| 0107 | [Case Workflow — the Cases Area](./0107-case-workflow-cases-area.md) | Accepted | 2026-07-20 | cases, decision-console, irene, shirley, planning-desk, journal, workflow, audit-trail, agpl-release-scope |
| 0108 | [In-Repo Licensing and Contribution Apparatus](./0108-in-repo-licensing-and-contribution-apparatus.md) | Accepted | 2026-07-29 | licensing, agpl, cla, trademark, contribution, agpl-release-scope |
| 0109 | [Lint, Typecheck and CI Contract](./0109-lint-typecheck-and-ci-contract.md) | Accepted | 2026-07-29 | process, tooling, lint, format, typecheck, ci, agpl-release-scope |
| 0110 | [Typing Island Set at CI Landing — Analytics Deferred](./0110-typing-island-set-analytics-deferred.md) | Accepted (supersedes ADR-0109 §3 in part) | 2026-07-30 | process, tooling, typecheck, ci, agpl-release-scope |
| 0111 | [Retire the Four Placeholder Sections in Front Office and Back Office](./0111-retire-placeholder-sections.md) | Accepted | 2026-07-31 | frontend, ui, web, ia, sections, front-office, back-office, module-specs, agpl-release-scope |
| 0112 | [Scoped Settings & Credential Architecture — Application / Tenant / User](./0112-scoped-settings-and-credential-architecture.md) | Accepted | 2026-08-03 | configuration, security, multi-tenancy, credentials, llm, telegram, voice, admin, compliance, audit, deployment |
| 0113 | [Front-Office Charts — Unified Axis End, Plan-Tail Display, and Hero De-Clipping](./0113-front-office-charts-unified-axis-end-and-plan-tail-display.md) | Accepted | 2026-08-05 | charts, front-office, plotly, plan, time-axis, benchmarks, ux |
| 0114 | [Chart Snapshot Persistence — Session Rehydration and Case Pinning](./0114-chart-snapshot-persistence-chat-and-cases.md) | Accepted | 2026-08-05 | architecture, ui, cases, shirley, persistence, audit |
| 0115 | [Rename the Decision Console Area to Watch Desk](./0115-watch-desk-rename.md) | Accepted (2026-08-11) | 2026-08-10 | watch-desk, decision-console, naming, ui, homepage, agpl-release-scope |
| 0116 | [Watchpoint Registry and Signal Families for the Watch Desk](./0116-watchpoint-registry-and-signal-families.md) | Accepted (2026-08-11) | 2026-08-10 | watch-desk, irene, watchpoints, calibration, audit, price, fx, liquidity, freshness, htmx, agpl-release-scope |
| 0117 | [Built-in Tick Scheduler — In-Process Default with External Opt-out](./0117-built-in-tick-scheduler.md) | Accepted (2026-08-11) | 2026-08-11 | watch-desk, irene, market-data, scheduling, deployment, operations, multi-tenancy |
| 0118 | [Voice Providers in the Scoped-Settings Taxonomy — Per-Tenant Voice Credentials & Settings](./0118-voice-providers-in-the-scoped-settings-taxonomy.md) | Accepted (2026-08-12) — annex amendment to ADR-0112 §3 | 2026-08-12 | voice, configuration, security, multi-tenancy, credentials, admin, telegram, shirley |
| 0119 | [Watch Desk Cadence Vocabulary v1, Anchor Semantics, and Irene Schedule Seeding](./0119-watch-desk-cadence-vocabulary-and-seeding.md) | Accepted (2026-08-13) — extends ADR-0086's v0 cadence vocabulary | 2026-08-13 | watch-desk, irene, scheduling, cadence, seeding, multi-tenancy, timezone, ui |
| 0120 | ["Open case →" Gated by Band, Not by Options Presence](./0120-case-affordance-band-gate.md) | Accepted (2026-08-13) — revises ADR-0107 D1's placement rule for the option-less case | 2026-08-13 | cases, watch-desk, irene, ui, htmx, bait-vait, findings |
| 0121 | [Tenant-Scoped User Management with Owner-Gated Admin Surface](./0121-tenant-scoped-user-management-and-owner-gated-admin-surface.md) | Accepted (2026-08-14) | 2026-08-14 | users, roles, permissions, admin, multi-tenant, rls, audit, sessions, release |
| 0122 | [Sidebar Area Order v3](./0122-sidebar-area-order-v3.md) | Accepted (2026-08-15) — supersedes the sidebar-order statement in ADR-0104 §6 | 2026-08-15 | ui, navigation, shell, sidebar, information-architecture, areas |
| 0123 | [Report Scraper Model in the Scoped-Settings Taxonomy — Per-Tenant Resolution for the One-Shot Extraction Path](./0123-report-scraper-model-in-the-scoped-settings-taxonomy.md) | Accepted (2026-08-15) — annex amendment to ADR-0112 §3; amends ADR-0053's model dropdown | 2026-08-15 | scraper, configuration, multi-tenancy, credentials, admin, openrouter |
| 0124 | [Installation and Release Distribution — Guided Installer, Engine-Neutral Bootstrap, and the `stable` Branch](./0124-installation-and-release-distribution.md) | Accepted (2026-08-19) — amends ADR-0040 (operator entry point only) and the dev-only password note in `db/init/01-create-app-role.sql` | 2026-08-19 | installation, release, packaging, operations, ci, developer-experience |
| 0125 | [Sub-Hourly Market-Data Refresh Cadence, Kind-Aware Fetching, and On-Demand Refresh Feedback](./0125-market-data-refresh-cadence-and-on-demand-feedback.md) | Accepted (2026-08-22) — extends ADR-0119's cadence vocabulary and revokes its market-data choice-list statement; changes ADR-0093's seeded cadence value only | 2026-08-22 | market-data, scheduling, cadence, admin, front-office, htmx, owner-gating, deploy |
| 0126 | [Owner-Gating of the Market Data Admin Section](./0126-owner-gating-of-the-market-data-admin-section.md) | Accepted (2026-08-23) — supersedes one sentence of ADR-0125 §6; applies the ADR-0121 §6 owner-gating pattern | 2026-08-23 | admin, market-data, owner-gating, roles, permissions, htmx, security |
| 0127 | [Temporal Grounding — Current-Date Injection and Actuals-First As-Of Default for Limit Coverage](./0127-temporal-grounding-current-date-injection-and-actuals-first-limit-coverage.md) | Accepted (2026-08-24) — corrects the tool-default consequence of ADR-0103 §2's horizon range resolution without editing it; extends ADR-0012 B8 prompt grounding | 2026-08-24 | shirley, ai-service, prompt, tools, limits, temporal, back-office, telegram |
| 0128 | [Transactions Area — Trade-Ticket Object Model and Record Flow](./0128-transactions-area-trade-ticket-object-model-and-record-flow.md) | Accepted (2026-08-27) — extends the investment domain of ADR-0097/0098 with a layer *above* the ledger (ledger and materialisation unchanged); leaves the ADR-0104 §2 overlay contract untouched; adds Transactions as the ninth Area | 2026-08-26 | transactions, trade-ticket, area, schema, cash-settlement, rls, four-eyes, provenance |
| 0129 | [Provider Channel — Suggestion List, Zero-Knowledge Relay, Provider Portal, and Engagements](./0129-provider-channel-suggestion-list-relay-portal-and-engagements.md) | Accepted (2026-08-27) — revives the provider-directory half of the Execution-Network concept ADR-0107 cut, under the conditions ADR-0107 named; honours the ADR-0108 open-client / proprietary-service split | 2026-08-26 | provider-channel, suggestion-list, relay, encryption, engagement, monetisation, agpl-boundary, regulatory |
| 0130 | [Non-Negative Holdings Guard: Cash Investments Are Exempt](./0130-non-negative-holdings-guard-cash-investment-exemption.md) | Accepted (2026-08-31) — supersedes the *mechanism sentence* of ADR-0128 Q-2 (path-scoped capability flag); narrows the ADR-0097 §4 write-time invariant to non-cash investment types; ADR-0128 Q-2's behavioural decision stands | 2026-08-31 | cash, holdings, ledger, invariant, transactions, crud, excel-import, overdraft |

> **Number-collision resolved (2026-06-03 reconciliation):** the file formerly
> at `0069-single-investment-review-web-surface.md` was renumbered to **0073**
> (status corrected to `Accepted`); `0069-shirley-back-office-analysis-tools.md`
> retains 0069. The next free ADR number is **0085**. Residual ADR-0069
> cross-references in code/test *comments* that pointed at the renumbered ADR
> are listed for update in `docs/_audit/doc-code-reconciliation-2026-06-03.md`
> §A (operator action — outside this documentation pass). **Update (2026-07-02):**
ADRs 0085–0089 (Irene / Decision Console, Feature #033) are filed as
`Proposed`. ADRs 0090–0093 (Live Data Import — provider-agnostic ingest)
were **Accepted** on 2026-07-06 (roadmap #036 in-progress). ADR-0094 (GUI
Sunset Execution, roadmap #016) is filed as `Proposed`. **Update
(2026-07-07):** ADR-0095 (Provider Credential Vault, roadmap #037 —
Provider Credential Management) and ADR-0096 (Identifier Scheme-Set
Extension, roadmap #036 — Live Data Import) were **Accepted** on
2026-07-07. **Update (2026-07-08):** ADR-0097 (Position Model —
transaction ledger, holdings derivation, valuation modes, instrument
prices) and ADR-0098 (Computed-NAV materialisation and live-ingest
write-path re-routing), both roadmap #038 — Position Model, were
**Accepted** on 2026-07-08. **Update (2026-07-10):** ADR-0099
(multi-currency model — functional currency, `fx_rates`, conversion
boundary) and ADR-0100 (explicit foreign-currency cash positions and
the redefined cash residual) are filed as drafts. ADR-0100 depends on
ADR-0099 and **amends** ADR-0055 (cash as residual), which keeps its
`Accepted` status — the residual mechanism is narrowed, not replaced.
**Update (2026-07-11):** ADRs 0085–0089 (Irene / Decision Console, #033),
ADR-0094 (GUI Sunset Stage 1, #016), and ADRs 0099–0101 (multi-currency
programme, migrations b026/b027) were set to **Accepted** against the
shipped code. ADR-0094 is accepted for **Stage 1 only** — its §5 Stage 2
(the DataStore complex) remains open and is tracked as roadmap #035.
ADR-0102 (statistics / SAA / benchmark currency contract, roadmap #040) —
the successor decision ADR-0099 §6 deferred — was likewise **Accepted**
against the shipped code on 2026-07-11; the local-currency perspective it
declines to offer is raised as roadmap #045 (FX / asset-return attribution).
**Update (2026-07-13):** ADR-0103 (cash as a first-class asset class) and
ADR-0104 (plan-path materialisation and scenario overlay architecture — the
Planning Desk) were **Accepted ahead of implementation**, on the ADR-0090–0093
design-first precedent and as each ADR's §Operator action requires (both
accepted and registered before any implementation prompt is produced). They are
tracked as roadmap **#048** (cash) and **#049** (Planning Desk, which #048
blocked). *(Superseded by events: both have since shipped — #048 on 2026-07-13,
and the Planning Desk build-out through the ADR-0104/0105 v1 horizon in July
2026.)* ADR-0103 **amends** ADR-0100 and
ADR-0055 — cash becomes wholly explicit and the `portfolio_aum` residual is
retired by forward migration, strictly *after* cash materialises correctly and
one reconciliation cycle passes. ADR-0104 **amends** ADR-0089 — the Decision
Console's §Scenarios placeholder anchor relocates to the new Planning Desk area
(the seventh Area) — and **commissions** two ADRs it does not write: ADR-D
(Takahashi–Alexander engine, roadmap #023) and ADR-E (scenario regimes, roadmap
#034). The next free ADR number is **0106**.

**Update (2026-07-15):** ADR-0105 (Takahashi–Alexander pacing profiles),
commissioned by ADR-0104 as "ADR-D", was **Accepted ahead of implementation**
on the same design-first precedent, and is tracked as the TA slice of roadmap
**#023**. It ships no schema; its generator is pure and its profiles ephemeral.

**Update (2026-07-20):** ADR-0106 (Decision Console `options` rendered as
decision-support prose, DC3 of the "One Glass" refresh) was **Accepted**.
It **refines** — does not amend or supersede — ADR-0088: the `options` wire
contract stays `array<string>`, band-gated; only the authored/rendered form
(short prose, the "Possible moves" block) changes. No schema, no migration.

**Update (2026-07-20, second entry):** ADR-0107 (Case Workflow — the Cases
Area) was **Accepted ahead of implementation**, on the same design-first
precedent as ADRs 0103–0105. It **refines** ADR-0085 (adding one resolution
member, `opened_case`; ADR-0085 stays Accepted and unedited) and arms the
disabled "Open case →" affordance that ADR-0106 ships. It **commissions**
roadmap **#050** (Irene price-movement watch family, `price:*`), and is itself
tracked as roadmap **#051** (raised 2026-07-20 — the entry ADR-0107 §Implements
calls for). ADR-0107 adds the **eighth** Area, after Decision Console (sixth,
ADR-0089) and Planning Desk (seventh, ADR-0104 §6); the "seven Areas" wording in
CLAUDE.md, `docs/architecture.md`, and the ADR-0084 glossary is updated **with
#051's implementation, not before it**.
The next free ADR number is **0109**.

**Update (2026-07-29):** ADR-0108 (in-repo licensing and contribution
apparatus) and ADR-0109 (lint, typecheck and CI contract) were **Accepted**
— both are AGPL-public-release scope: ADR-0108 implements roadmap **#052**
gate 4 (licensing apparatus), ADR-0109 is tracked as roadmap **#054**, which
blocks #052 gate 5 (CI green on `main`). ADR-0109 deliberately
delegates its living inventory — the staged-adoption `ignore` list and the
DataStore per-file-ignores — to `[tool.ruff.lint]` in `pyproject.toml`, so
the accepted ADR needs no retroactive edit when the adoption run fills those
lists; the DataStore block rides #035 (ADR-0094 §5), the staged entries ride
#054's post-release note. It records one mechanical `ruff format` commit as a
history event (`.git-blame-ignore-revs`), and its `full-suite.yml` nightly
cron is activated only with the public flip (#052 gate 8).
The next free ADR number is **0110**.

**Update (2026-07-30):** ADR-0110 (typing island set at CI landing —
analytics deferred) was **Accepted**, tracked as roadmap **#054** and
**superseding ADR-0109 §3 in part** — the island set only; every other
section of ADR-0109 stands, and it remains the CI contract. The adoption
run measured what ADR-0109 §3 could not know in advance: `services/overlay/`
and `services/market_data/` reach zero pyright findings, but
`services/analytics/` carries **163**, all artefacts of pyright inferring
pandas types from source (pandas ships no `py.typed`) and none a real
defect. Rather than suppress 163 findings through the purity-guarded
calculation core — which would have made pyright-green mean
"zero-after-suppression" instead of "zero" — analytics leaves the island
set and re-enters via `pandas-stubs` as the first entry of #054's
post-release typing note. ADR-0110 also sanctions the inline
`# type: ignore` with a stated reason for imports unresolvable by
construction (the optional proprietary `blpapi` SDK), over any
config-level blanket suppression.
The next free ADR number is **0111**.

**Update (2026-07-31):** ADR-0111 (retire the four placeholder sections in
Front Office and Back Office) is `Accepted`. It comes out of the
operator walkthrough for the UI Polish Pass (roadmap **#053**) and is tracked
there as findings **UI-S01** (Front Office: `export`, `timeseries`) and
**UI-S02** (Back Office: `cashflow`, `portfolio-tracking`) — AGPL-public-release
scope, since a "planned" pill for abandoned work is visible to reviewers of the
public release. Its §Decision 2 states the **post-cut section topology** for the
two areas as authoritative — Front Office `overview`, `charts`, `statistics`,
`portfolio-optimizer`; Back Office `saa`, `benchmarks-attribution`, `limits` —
so the section catalogue in `web/shell.py` and the body partials must move
together to keep the ADR-0058 catalogue guard green. It retires placeholders
only: no functionality is built or relocated, no schema, no migration. The four
never-built module specs are **archived**, not deleted, to
`docs/_archive/module_specs/`.
The next free ADR number is **0113**.

ADR-0112 (Scoped Settings & Credential Architecture) is Accepted as of
2026-08-03. It partially supersedes ADR-0095: §4 of that ADR (the Stage-2
`provider_credentials` storage design) is replaced by ADR-0112 §2's
`scoped_settings` table, while ADR-0095 §1–§3 (resolution contract, fallback
policy, environment source) remain authoritative. It also supersedes the
ADR-0052 persistence posture ("runtime edits live in the process only; .env is
canonical") once ADR-0112's management surface lands (strand F3).

**Update (2026-08-05):** ADR-0113 (Front-Office Charts — unified axis end,
plan-tail display, and hero de-clipping) is **Accepted**. It supersedes
nothing: every tile in the Charts section gains one shared right-hand axis end
(the tenant's newest actual NAV date, with the start left data-driven), the two
NAV-space tiles gain a dashed plan-tail trace from the last actual to that end,
and the listed heroes stop clipping their cumulative lines to the benchmark
inner-join at the right edge. Analytics stay actual-only and no gap is
zero-filled — an investment without plan rows shows its line ending early, the
honest gap rather than a fabricated continuation.

**Update (2026-08-05):** ADR-0114 (Chart Snapshot Persistence — session
rehydration and case pinning) is **Accepted**. It resolves the artefact-
rehydration strand ADR-0050 deferred: Shirley's Plotly specs are archived as
**frozen snapshots** on two seams — a per-session sidecar beside the chat
history (never inside it, so the spec never re-enters the model's token
stream), and `chart_snapshot`, the fourth pin artefact class on the Cases
journal after `document`, `scenario_snapshot` and `consultation`. Nothing is
replayed or recomputed on either surface; the pin transports the sidecar
`artifact_id` by reference and the server embeds the resolved spec, so the case
record stays self-contained. A 1 MiB `_CHART_SPEC_BYTE_CAP` at the single
capture point degrades to a calm placeholder without ever refusing the live
render. No new table and no migration — the chat seam is in-memory (ADR-0050's
contract, migration trigger 2 unchanged), the case seam reuses the existing
journal-entry JSONB payload.
The next free ADR number is **0115**.

**Update (2026-08-10):** ADR-0115 (rename the Decision Console Area to Watch
Desk) and ADR-0116 (watchpoint registry and signal families for the Watch Desk)
are filed as `Proposed`, both AGPL-public-release scope (#052) — the rename is
deliberately taken before the flip, while the slug, the module path and the docs
are not yet public API. ADR-0116 **depends on** ADR-0115 and uses the new names
throughout; the rename is prompt P1 of the joint programme, so ADR-0116's
implementation (P2–P6) never needs a re-touch pass. Both **refine** rather than
amend: ADR-0115 refines ADR-0089 (the area it renames), ADR-0106 (structure
untouched) and ADR-0107 (the Cases hand-off gains a clearer division of labour);
ADR-0116 refines ADR-0089 (the Calibration section grows into the calibration
editor its own text left open) and ADR-0106 (the monitor gains editability and
new groups, its honesty rules generalised, not weakened). Accepted ADRs
0085–0114 keep saying "Decision Console" — they are immutable, and ADR-0115 is
the bridge a reader needs; the index rows above are likewise not retro-edited.
ADR-0116 ships migration **b033** (`watchpoints` + `floor_calibration`, both
historised by `effective_from` per ADR-0056, RLS'd and audit-triggered) and
explicitly declines `scoped_settings` (ADR-0112 §2) as their home — ADR-0112 is
untouched. It **commissions** four successors it does not design: class-level
`price` selectors, book-derived `fx` pair sets, `price` direction
configurability, and a `pacing:*` family strictly after the TA engine (#023,
Strand 3). It is also the concept roadmap **#050** (the `price:*` watch family
commissioned by ADR-0107, filed with "own concept at kickoff" and no ADR) was
waiting for — `price` is one of ADR-0116's four signal families; the roadmap
entry itself is reconciled with the implementation, not here.
The next free ADR number is **0117**.

**Update (2026-08-11):** ADR-0117 (built-in tick scheduler — in-process default
with external opt-out) is **Accepted (2026-08-11)**. It fills the
**tick-source seam** ADR-0086 drew and ADR-0093 reused 1:1 — both described
that seam as explicitly swappable "without touching domain logic", and both
shipped v0 against an external systemd timer. ADR-0117 takes it up: an asyncio
tick task in the web lifespan becomes the **default** tick source, the per-tick
orchestration is extracted into one engine-parametrised runner that the CLI
ticks become thin wrappers over, and the systemd units under `docs/deploy/` are
demoted from "the deployment" to a documented opt-out
(`TICK_SCHEDULER_ENABLED=false`) — the units themselves untouched. It
**refines** rather than amends: ADR-0086 and ADR-0093
remain immutable and correct, their due-evaluation and advisory-lock
architecture is unchanged, and per-tenant cadence stays exactly where
ADR-0085/0086 put it — this ADR changes only *what ticks*, never *who is due
when*. Coexistence is safe by construction, since `pg_try_advisory_xact_lock`
deduplicates internal and external claimants whichever fires first. It extends
the audit-engine **sanctioned-path set** (the ADR-0036 / ADR-0063 / ADR-0064 /
ADR-0112 lineage) with a **fifth** path, on the precedent of path 4 (ADR-0112
§5, the Telegram bot-token scan): the cross-tenant due reads, asserted
read-only and confined to the two schedule tables. That is the whole
superuser-privileged surface — the runner takes **one** engine and carries the
per-tenant transactions on it as well, but `tenant_context(enforce_rls=True)`
switches each of those to the unprivileged `APP_DB_ROLE` (ADR-0078), so RLS is
enforced regardless of the role the engine connects as, exactly as the CLI
ticks' superuser engine relies on today. Which tick source runs is
**deployment-scope configuration** — environment, not database and not UI (the
same scope position `.env` holds in the ADR-0112 chain); the UI gains the
scheduler's *health*, not its settings. No schema and no migration. Its roadmap
home is **#058**.
**Update (2026-08-12):** ADR-0118 (voice providers in the scoped-settings
taxonomy — per-tenant voice credentials & settings) is **Accepted
(2026-08-12)**. It is an **annex amendment to ADR-0112 §3**: the taxonomy
table's `voice_stt` / `voice_tts` row read "pinned application(env) in v1,
taxonomy-extensible later", and this is that extension. ADR-0112 itself
remains immutable and otherwise unchanged; ADR-0076 stays authoritative for
the audio pipeline, the provider protocol and the per-call client lifecycle,
with only its configuration posture superseded for the tenant-facing fields
declared here. The taxonomy gains **three** declarations, all tenant-scope,
`env_fallback=True`, `optional=False`: `voice` (config-only, `enabled`),
`voice_stt` (`api_key` secret; `model`, `base_url`) and `voice_tts`
(`api_key` secret; `model`, `voice`). The split is forced by ADR-0112 §1's
unit-chaining rule — one provider carrying both keys would drag them into a
single scope and destroy ADR-0076's STT/TTS provider-mixing freedom (Groq STT
via `base_url` alongside OpenAI TTS). A `ResolvedVoice` value object with a
masked repr mirrors `ResolvedLLM`; both surfaces (web `/chat/voice` and
`/chat/tts`, the Telegram voice handler) resolve **per request** inside the
tenant context, `voice_enabled` becomes a per-tenant answer computed per
render and per turn, and the module-level singletons in `services/voice/` are
retired so the resolver's environment source is the single application-scope
path. `optional=False` is deliberate: enablement and credential presence stay
separate questions, so an enabled-but-keyless tenant fails loudly at first use
rather than degrading to a silent off. The Admin surface gains three
taxonomy-driven cards with "live" pills and no bespoke settings page; there is
no schema change and no migration. Its roadmap home is **#059**.

**Update (2026-08-13):** ADR-0119 (Watch Desk cadence vocabulary v1, anchor
semantics, and Irene schedule seeding) and ADR-0120 ("Open case →" gated by
band, not by options presence) are both **Accepted (2026-08-13)**. Neither
ships schema, and neither needs a migration. ADR-0119 **extends** ADR-0086
without amending it: the v0 vocabulary `{daily}` grows to `daily · hourly ·
every_2h · every_3h · every_6h`, validation stays sole-sourced in
`compute_next_due_at`, and the tick/due split ADR-0086 drew — reused 1:1 by
ADR-0093 and taken up as the in-process default by ADR-0117 — carries the new
members unchanged, since both tick sources (60 s internal, 15 min external)
remain finer than the finest cadence. `preferred_hour` is reinterpreted as the
**anchor hour** for the interval cadences (`(anchor + k·N) mod 24` in the
schedule's IANA zone), with `daily` the N=24 case and therefore behaviourally
untouched. Its second half closes a **cold-start** gap in ADR-0093's seeding
(STD-03): `seed_tenant_defaults` gains an idempotent `irene_schedule` row,
seeded **enabled** — deliberately asymmetric to the disabled market-data row,
because the Irene domain is gated on a resolvable LLM credential and skips
quietly per tick until one exists, so a fresh tenant gets a live Watch Desk
rather than a dead-looking area. It also commissions a roadmap item it does not
design: Watch Desk **area-level deactivation**. ADR-0120 **revises** ADR-0107
D1 (Gate-C0 A) for the option-less case, leaving ADR-0107 itself Accepted and
unedited: the shipped `{% if card.options %}` template gate was stricter than
the decision it implemented, since `options` is optional in ADR-0088's
`surface_finding` contract, so a critical finding phrased as pure statement
carried no case path at all. The affordance now follows the **band** —
`noteworthy` / `critical` render it, informational never does — placed inside
the Possible-moves block when present and as a slim footer variant when absent,
with the C4 endpoint enforcing the same gate server-side. ADR-0088's payload is
untouched.

**Update (2026-08-14):** ADR-0121 (tenant-scoped user management with an
owner-gated Admin surface) is **Accepted (2026-08-14)**. It closes the last gap
in the ADR-0063 / ADR-0064 user story: the platform level was complete — a
super-admin creates a tenant with its first owner atomically, in UI and CLI —
while the tenant level had no in-product path at all, so every further user
required the operator CLI. A new `services/tenant_users/` package runs list,
create, deactivate, reactivate, reset-password and role-change **under the app
role inside `tenant_context`**, so tenant confinement comes from RLS and actor
attribution from the `users_audit_trigger` reading the `app.user_id` GUC — no
new audit code, and explicitly *not* the superuser engine, which stays the
super-admin path's alone. The web surface is a **Users** section in the tenant
Admin area built on the ADR-0112 §6 render-path idiom (one `_render_section`
helper for GET and every POST re-render, rejected writes answering 400 with the
same body), gated by the existing `require_role("owner")` with its plain-403
semantics unchanged — the route is authoritative, the template only mirrors.
Guards live in one helper inside the writing transaction: last-active-owner
protection against both deactivation and demotion, no self-deactivation,
self-demotion allowed as the hand-over case, owners as peers, and session
invalidation on deactivation and password reset. `_validate_email` /
`_validate_roles` are extracted out of `services/super_admin/operations.py` and
shared rather than forked, and one password policy
(`validate_password_strength`) governs both new paths — with the documented,
accepted asymmetry that `create_user_idempotent` still does not enforce it, a
follow-up rather than a pre-release behaviour change on a working operator
path. `auditor` remains declared-but-dormant and is not offered in the UI.
There is **no schema change and no migration** — `users.roles`, `is_active`,
`display_name` and `password_hash` all exist — and the super-admin per-tenant
user view (U3) is deliberately deferred past the AGPL release. Its roadmap home
is **#015 sub-item B1f**; no new roadmap ID is issued.

**Update (2026-08-15):** ADR-0122 (sidebar Area order v3) is **Accepted
(2026-08-15)**. It **supersedes the sidebar-order statement in ADR-0104 §6**,
leaving ADR-0104 itself Accepted and unedited per the immutability discipline.
The new order is Front Office → Back Office → Assistants → Planning Desk →
Investor Communication → Watch Desk → Cases → Admin: the book first, the
assistant as the standing companion right behind it — Shirley is the primary
interactive companion for day-to-day work on Front- and Back-Office data, and
seventh position undersold that — then the forward-looking planning surface and
the outward-facing communication surface, then the monitoring-and-exception
workflow, which is consulted on its own beat rather than navigated to
constantly, with Admin last. Watch Desk and Cases stay **adjacent**, preserving
the ADR-0107 pairing. This is a pure reordering: labels, slugs, URLs, section
catalogues and area body partials are untouched, and there is no migration and
no behavioural change beyond navigation order. The order is duplicated by design
in `web/shell.py` `_AREAS` and the hardcoded `_areas` list in
`web/templates/_partials/sidebar.html`, with one hard order pin in
`tests/web/test_sidebar_glyph_and_auth_polish.py`; every other consumer derives
from `all_areas()`. A rename of the **Assistants** area label to "Shirley" was
considered alongside the reorder and **rejected** (§3) — the area is the home
for assistive functions in general, already hosting the Report Scraper and the
Providers & Credentials pointer tile.

**Update (2026-08-15):** ADR-0123 (Report Scraper model in the scoped-settings
taxonomy) is **Accepted (2026-08-15)**. It is an **annex amendment to ADR-0112
§3** — one new non-secret `openrouter.scraper_model` config field, tenant scope
only, mirroring `irene_model`, with no new provider, no new secret and no
migration — and it **amends ADR-0053**'s model dropdown: the Scraper page no
longer offers a model selector, only a read-only line naming the model the next
run will use and pointing at Admin → Providers & Credentials. It closes the last
unconverted **web** consumer of the process-global `AIServiceCore` singleton left
open by ADR-0112 §4b / strand F4, which made the Report Scraper fail with
`AIServiceCore not connected` on every deployment whose OpenRouter key lives in
the vault rather than in `.env` — the multi-tenant default — while Shirley on the
same tenant worked. Resolution is per run, inside the requesting session's
`tenant_context`: the credential through `resolver.resolve()` unchanged, and the
model through `tenant scraper_model → tenant model → env SCRAPER_MODEL → env
SHIRLEY_MODEL → _DEFAULT_SCRAPER_MODEL`, the exact shape of the Irene chain, so
cost attribution follows the tenant's key. `send_one_shot_extraction` gains the
same `llm | model` mutual-exclusion seam `run_synthesis` already carries, and
`ScraperService.scrape_reports` takes a `ResolvedLLM` in place of a model string
while staying DB-free, FastAPI-free and Qt-free. ADR-0027's capability gate is
**unchanged** and still runs before any file is touched — an unsupported
resolution is a loud, operator-readable refusal, never a silent fallback — and
resolution is **never stashed** (chat's D3): the POST resolves to fail fast, the
SSE stream resolves again and that second resolution drives the run. Once this
lands, `web/main.py`'s parked application-scope singleton has exactly one
remaining consumer, the Fetcher-LLM; converting it is explicitly **not** in scope.

**Update (2026-08-19):** ADR-0124 (installation and release distribution) is
**Accepted (2026-08-19)**. It closes the installation gap left open by the
2026.08.0 public release: the README's manual sequence assumed a container
engine, a Compose provider and (on macOS) a running Podman machine, none of
which were checked, and pinned a literal version tag that had to be hand-edited
per release. Four decisions. (1) `scripts/db-init.sh` and `db-reset.sh` resolve
engine and Compose provider **once** into two indexed arrays (`--engine` flag →
`PORTFOLIFLOW_ENGINE` → Podman → Docker); a resolved engine with no working
Compose provider is a hard error naming the packages that would fix it, never a
silent fallback. `db/init/01-create-app-role.sql` becomes a `.sh` so the
application-role password can come from the container environment — the literal
default and its "DEV ONLY" note are carried over verbatim, so existing checkouts
behave exactly as before, but overriding no longer means editing a tracked file.
The role keeps its non-negotiable properties: no `SUPERUSER`, no `BYPASSRLS`.
`POSTGRES_PORT` is parameterised, both bind mounts gain `,Z`, and the GNU-only
`head -n -N` help rendering goes. (2) `scripts/install.sh` is a guided,
single-file installer targeting **bash 3.2 and BSD userland**, in remote or local
mode — remote clones and then `exec`s the *cloned* copy, so the code that
installs is always the code being installed. Six phases: preflight, fetch,
runtime, configure, database, summary; Phase 4 **delegates** to `db-init.sh`
rather than reimplementing it, so container-start / wait / migrate / bootstrap
stays one implementation. It generates per-installation secrets (Postgres
superuser, application role, `CREDENTIAL_VAULT_MASTER_KEY`) and **never invokes
`sudo`, never installs system packages, and never writes outside the target
directory** — a missing prerequisite prints the exact package-manager command and
exits non-zero. Prompts read from `/dev/tty`; `--doctor` runs preflight and
verify only. (3) The one-liner is `bash -c "$(curl -fsSL …/install.sh)"`, not a
pipe, because with a pipe stdin *is* the script and the prompts would eat it;
`https://portfoliflow.com/install.sh` redirects to the `stable` branch on
`raw.githubusercontent.com`, keeping the repository the single source of truth
and the redirect a kill switch, with a `install.sh.sha256` release asset and a
documented download-inspect-verify-run path of equal prominence. (4) **`stable`
is a branch, not a moving tag** — `git fetch` does not update an existing tag
without `--force`, so a moving tag would leave users silently on different trees;
a branch clone also yields a tracking ref, making updates a `git pull`. A
`promote-stable.yml` workflow advances it on every non-pre-release tag and fails
the release if `pyproject.toml` disagrees with the tag. A weekly `installer` CI
job exercises Ubuntu/Docker and macOS/Podman end-to-end plus the idempotence
path, because installer rot comes from the outside world, not from commits here.
Explicitly **not in scope**: the hard-coded primary tenant and `portfoliflow_dev`
database name (a product decision with its own ADR), Windows beyond WSL2, OS
packages and PyPI publication, production deployment concerns, an in-place
`--upgrade` path, and GPG-signed tags. Four implementation strands, I1 → I2 →
I4 with I3 independent.

**Update (2026-08-22):** ADR-0125 (sub-hourly market-data refresh cadence,
kind-aware fetching, and on-demand refresh feedback) is **Accepted
(2026-08-22)**. The recurring-refresh machinery existed end to end but was not
*perceived*: the cadence vocabulary was defined in whole hours, STD-03 seeded
`daily · 06:00`, every run fetched every ingestable kind, and "Refresh now"
reported "queued" and never reported landing. Eight decisions. (1)
`_CADENCE_INTERVAL_HOURS` becomes a `timedelta`-valued map gaining `every_30m`
and `every_15m`; ADR-0119 §2's `anchor + k·step` arithmetic already yields the
quarter-hour grid, so anchor semantics are unchanged and the anchor hour is
accepted as practically inert for the new members. (2) The choice lists
**diverge per domain** — the Watch Desk keeps `daily … hourly` (an Irene beat
every 15 minutes is an LLM-cost decision it has not taken, pinned by a test
that its tuple does not grow), while market data offers
`every_15m · every_30m · hourly · daily` finest-first with a label map. (3)
STD-03 seeds `every_15m · preferred_hour = 0`, still `enabled = FALSE`
(ADR-0093 unchanged) and still insert-if-absent: **no backfill**, because an
existing row cannot be told apart from one an owner deliberately left at
`daily`. (4) `refresh_tenant_live_data` splits ingestable kinds — `nav_price`
every run, every other kind only on the first run of each UTC calendar day —
derived from fields the tick already passes, so no schema, runner-interface or
report change. (5) The Admin confirmation gains the ADR-0120 poller one-for-one
(`GET …/refresh/poll?since=`, 204 pending / 286 done / 286 + `HX-Reswap: none`
on malformed input, 15-second interval, started only from the confirmation).
(6) The Overview's `.ov-meta` line becomes the book's freshness stamp for all
members, with an owner-gated `Refresh` affordance whose 286 swaps
`#ov-section-body` as `outerHTML` — the whole body, because the reason to
refresh is to see the numbers move — and the owner gate is enforced
**server-side on refresh-now**, not only in the template. (7) Rendering cost
stays bounded by construction: no page polls on cadence, and the only added
render is one Overview body per *manual* refresh. (8) The external tick
template moves to `OnCalendar=*:0/5` so ADR-0086's "tick finer than finest
cadence" still holds for opt-out deployments; the built-in scheduler's
60-second default already satisfies it. No migration. Two roadmap items
registered — **#063** trading-hours awareness and **#064** provider retry
policy in the tick. Four implementation strands (M1 → M2/M3/M5; M4, startup
refresh, was assessed and dropped before acceptance, the numbering gap kept
deliberately).

**Update (2026-08-23):** ADR-0126 (owner-gating of the Market Data admin
section) is **Accepted (2026-08-23)**. ADR-0125 §6 owner-gated "Refresh now"
on the premise that "nothing observable changes in Admin, which is already an
owner surface under ADR-0121" — a false premise: ADR-0121 made the **Users**
section owner-only, while the Admin *area* renders for every authenticated
tenant user and `is_tenant_owner` is cosmetic mirroring. Three live
consequences: `POST /api/market-data/schedule` carried no role gate, so any
member could change the tenant's cadence, anchor hour, timezone and enabled
flag — a tenant-level setting that spends the tenant's provider budget; the
section rendered for members with a "Refresh now" button that silently did
nothing (the route 403s, HTMX swaps nothing on 4xx); and
`README-market-data-tick.md`'s "an owner opts in" was not enforced for the
opt-in itself. Four decisions, following the ADR-0121 §6 pattern. (1)
`save_schedule` gains `Depends(require_role("owner"))` — authoritative, at the
route layer — so both mutating routes of the module are gated. (2) The Market
Data include moves inside the same `{% if is_tenant_owner %}` conditional as
the Users section — cosmetic, at the template layer — and the dead affordance
disappears with the section. (3) `admin_view` calls
`load_market_data_section_context` only for owners and spreads an empty context
otherwise, because unlike Providers & Credentials and Users this section is
rendered **eagerly** in the `/admin` request; the missing keys are harmless as
the only consumer sits inside the owner conditional. (4) `GET
/api/market-data/refresh/poll` stays on `require_session` as a **deliberate
exception**: `require_role` would route every poll through
`require_authenticated_session` and its idle-timer touch, keeping an abandoned
tab's session alive — exactly what the ADR-0120 poll discipline prevents. The
cost is configuration cosmetics, not secrets, and the poll mutates nothing.
Members' freshness surface remains the Front Office Overview line (ADR-0125
§6). This ADR supersedes **one sentence** of ADR-0125 §6; the rest of that
section, including the owner gate on `refresh_now`, stands, and ADR-0125 is not
edited (ADR immutability). No migration.

**Update (2026-08-24):** ADR-0127 (temporal grounding — current-date
injection and actuals-first as-of default for limit coverage) is **Accepted
(2026-08-24)**. An observed Shirley dialogue answered a limits-headroom
question with figures that were internally consistent and entirely wrong for
the question: the reported Stichtag was **2030-12-31**, the end of the
planning horizon, presented as the current state of the book. A code review
found two independent causes. (1) `AIServiceCore.get_system_prompt` composes
the prompt from the soul fence, the generated tool inventory (ADR-0012 B8)
and the two context files — none of which carries a date, so the model cannot
classify a tool-reported Stichtag as past, present or future, nor notice the
contradiction. (2) `get_limit_coverage` passes `to_date=None`, which
`LimitsCoverageService._resolve_date_range` resolves to `max(as_of_date)`
across **both** NAV streams (ADR-0103 §2, correct for the Back Office
coverage view); with plan rows seeded through the horizon and `cut_over`
defaulting to today, every evaluation date past it draws from the plan
stream, and the summary then labelled plan projections "present and
historical". The review also established what this is *not*: there is **no**
shared as-of resolver and no platform-wide contamination — statistics,
portfolio review and the SAA comparison all pin actuals or today explicitly;
the defect is specific to the limits tool path. One decision in three
strands. **T1** prepends a two-line current-date block as the first content
of every composed prompt (and of the minimal fallback prompt, so grounding
does not depend on an intact soul file); both chat surfaces — web and
Telegram — pass through this one seam. Per-tenant timezone is out of scope
(the seam is synchronous and tenant-free); the Irene synthesis prompt is a
separate seam and out of scope. **T2** changes the **tool**, not the service:
an omitted `to_date` becomes `date.today()`, so the grid ends at the last
month-end Stichtag in actual territory, and a resolved Stichtag past the
cut-over is prefixed with an explicit plan-territory notice instead of the
blanket "present and historical" claim. Changing the service's own `None`
resolution was rejected — it would silently move the Back Office view's
default range in the same commit. The plan horizon stays reachable via an
explicit `to_date`. **T3** — labelling a plan-territory Stichtag in the Back
Office Limits KPI strip — is accepted in principle and **deferred** to a
roadmap entry; it is not a gate for `2026.09.0`. No migration; no engine or
service signature changes.

**Update (2026-08-27):** ADR-0128 (Transactions Area — trade-ticket object
model and record flow) is **Accepted (2026-08-27)**. Roadmap #061 required a
concept ADR settling four questions before any code: booked vs. proposed, the
boundary against the ADR-0097 ledger and the ADR-0104 overlay, the owning Area,
and what "analyse a change" resolves to. The gap it closes is concrete — the
manual ledger write is **single-leg** (`investments_add_position` writes one
`position_transactions` row and no cash leg), so after a real trade cash
correctness depends on the user entering a second, unlinked transaction by hand.
The decision introduces a tenant-scoped, RLS-protected **`trade_tickets`** table:
one object with one state machine
(`draft → proposed → approved → sent → acknowledged → executed → booked`, plus
terminal `cancelled`), because *booked* and *proposed* are two stations of one
lifecycle and the realised-vs-intended comparison is precisely the analytical
value. The object is named a **trade ticket** to avoid colliding with
`position_transactions` and with database transactions; the Area label stays
**Transactions**. A second table **`trade_ticket_effects`** enumerates the
emitted rows, so the ledger stays ignorant of the layer above it — no new column
on `position_transactions`, `investment_cashflows` or `investment_navs`, and
emitted rows reuse `ingest_origin='manual'` (Q-1), keeping the ADR-0092 triple
intact. The core mechanic is **two-leg atomic settlement**: instrument leg and
cash leg (price 1.0000 on the cash position of the instrument's currency) in one
DB transaction. Negative cash **warns, never blocks** (operator decision D-2,
Q-2): the oversell guard is lifted for `investment_type='cash'` only on the
ticket-emission path behind an explicit capability flag, and the negative balance
is a surfaced state until it returns to ≥ 0. Further decisions of record: a first
purchase of a new instrument is a `buy`, not an `opening` (R-1); partial secondary
sales are out of v1 (R-2); secondary-sale proceeds book as
`flow_type='distribution'` so DPI/TVPI stay truthful (Q-3); the price-plausibility
warning is a fixed 5 % constant, never a block (Q-4); "Book now" traverses
`proposed → approved → booked` implicitly, with the four-eyes columns present from
the first migration so enforcement is later a rule change, not a migration (Q-6,
D-4). Both boundaries hold: the ledger remains the single source of truth for unit
counts, and the pre-trade impact preview feeds a `proposed` ticket **read-only**
through the pure ADR-0104 executor — no overlay ever writes the book. After
`booked`, correction is **reversal, not mutation** (enumerated effects deleted in
one transaction, blocked if any effect row was modified since emission).
**Transactions joins the sidebar as the ninth Area**, between Cases and Admin,
completing the Watch Desk → Case → Transaction provenance chain; the eight→nine
documentation reconciliation (CLAUDE.md, `docs/architecture.md`, the ADR-0084
glossary) runs **with** the implementation, per the ADR-0107 precedent. Migration
number is claimed at implementation time. **Binding process note:** implementation
proceeds in operator-gated sub-strands with deliberate pause points — each surface
is discussed or mocked up before it is built.

**Update (2026-08-27):** ADR-0129 (provider channel — suggestion list,
zero-knowledge relay, provider portal, and engagements) is **Accepted
(2026-08-27)**. It is the companion to ADR-0128 and covers part (b) of roadmap
#061: the step *before* the booking — select a provider from a centrally curated
**suggestion list**, send an encrypted order or inquiry, receive a structured
confirmation whose fill data pre-fills the ADR-0128 booking step. It revives the
provider-directory half of the Execution-Network concept ADR-0107 cut, under the
conditions ADR-0107 named, and the red line is kept **structurally**: the
provider's `executed` message never books — it pre-fills, and a human confirms
every booking. Three constraints frame the design: the regulatory line
(PortfoliFLOW stays a software venue, never broker or advisor), the ADR-0108 AGPL
boundary (open client, message formats and verification keys in the repository;
the directory and relay are a separate, centrally operated service — the moat is
the network, not the code), and a self-hosting, data-sovereignty-motivated
audience for whom any phone-home must be radically opt-in. Staging is
**binding**: **Stage A** ships contract only, alongside ADR-0128 v1 — schemas,
directory format and signature-verification code, with the `sent` /
`acknowledged` / `executed` states unreachable; **Stage B** is the channel MVP
(directory + relay + portal, invited providers, engagements and order hand-offs);
**Stage C** is monetisation, behind a **hard gate: external legal counsel on the
intermediation question before any Stage-C design work**, and Stage B carries no
remuneration mechanics of any kind. Architecture: the directory is a static,
versioned, **signed** document (publishing key's public half in the AGPL
repository, successor-key rotation in the format) — key substitution via a
compromised fetch is the real attack surface, not the cipher — fetched only when
the tenant has enabled the channel, with suggestion filtering **client-side in the
instance** so the service never learns the portfolio or the query. The relay is
**zero-knowledge by design invariant**: ciphertext plus routing envelope only,
libsodium-family sealed boxes rather than OpenPGP, status polled by the instance
rather than webhooked into it (self-hosted instances behind NAT must not be
required to expose an inbound endpoint). The provider surface is a **web portal**
with client-side decryption — the provider's private key never reaches the server,
which shifts a documented key-backup burden onto providers — and **e-mail is
notification only, never transport**. **`engagements`** is the non-booking sibling
object (advisory / legal / fund-selection / second-opinion) on the identical
channel, with the ticket lifecycle minus the booking tail and an optional case
link carrying the provenance chain; Shirley remains the in-house first opinion, an
engagement is the external second one. The channel is opt-in per tenant, off by
default, an owner action; what leaves the instance is a version-pinned directory
fetch and per message the ciphertext plus envelope — **never** portfolio
composition, holdings, AUM or analytics. With the channel disabled, ADR-0128 is
fully functional, and no repository component gains a hard runtime dependency on
the central service. The same operator-gated, pause-point process as ADR-0128
binds Stage B.

The next free ADR number is **0130**.

**Update (2026-08-31):** ADR-0130 (non-negative holdings guard — cash
investments are exempt) is **Accepted (2026-08-31)**. The T-2/S2 verify-first
phase found ADR-0128 Q-2's *mechanism sentence* — a capability flag scoped to
the ticket-emission path — mechanically unable to deliver what the mockup
decision record MD-5 requires: "manual-ledger CRUD edits are never refused,
regardless of any cash balance". `first_negative_holding_date` scans the
**entire** candidate ledger, not the candidate row's marginal effect, so the
moment a ticket legitimately books a cash position negative — precisely the
behaviour Q-2 arms — the unconditional guard on the CRUD path refuses every
later add/update that does not cure the stretch from its first day, including
the canonical correction (a deposit dated *after* the stretch began) and
byte-identical restatements. It would close the ADR-0128 §6 correction path
exactly when a negative balance most needs correcting. The decision is
therefore principled rather than path-scoped: once a negative cash balance is
a permitted state of the book, a guard whose sole purpose is to reject
*impossible* states has no protective function on cash targets on **any** write
path. `InvestmentService.add_position_transaction` and
`update_position_transaction` skip the rejection when the target's
`investment_type` is `CASH_TYPE`, covering the ticket cash leg, the ADR-0097 §7
manual CRUD and the Excel cash-statement synthesis alike — one rule, no path
discrimination; **no capability flag is built** and the service signatures are
unchanged. Non-cash targets keep the ADR-0097 §4 guard unconditionally, so the
instrument leg of an emission is guarded with no exception. The pure helpers
(`first_negative_holding_date`, `derive_holdings`, `holdings_as_of`) keep their
semantics and their purity — the exemption is a service-layer decision about
*whether to raise* — and the `holdings_as_of` docstring caveat is corrected in
passing. Surfacing is unchanged: the composition-time `negative_cash` warning
and the persistent read-time indicator derived from the live balance, no stored
flag. Knowingly accepted: the Excel cash-statement synthesis may write a
negative balance — the import is the book of record and must mirror reality.
Per the immutability rule ADR-0128 is not edited; this ADR is the correction,
and future readers resolve the conflict in its favour. No migration; no schema
change.

The next free ADR number is **0131**.

**Phase 5 (Charts/Statistics web migration and analytics-service foundation) and Phase 6 Block 1 (frontend re-architecture) are complete. The web variant is the sole surface; the PyQt6 GUI was removed in the Qt sunset (ADR-0094 Stage 1, roadmap #016). Phase 7 (investment-limit monitoring, "Anlagegrenzen-Überwachung") shipped its data layer (ADRs 0055, 0056, 0057, migration b010), coverage engine, Excel-import path, and read-only web surface at `/back-office#limits` (roadmap B5 `mostly-done`); the editing surface is deferred.**

**For deferred items, the canonical source is `docs/roadmap.md`. For historical Phase-5 follow-ups, see `docs/_archive/phase-5-followups.md`.**

## Relation to Other Documentation

- **CLAUDE.md** — conventions and glossary enforced during AI-assisted development. Cross-reference relevant ADRs from there.
- **Docstrings** — document *what* a function or class does and how to use it. Cross-reference ADRs from docstrings where a non-obvious design choice is encoded in the code.
- **arc42 / architecture documentation** (if introduced) — describes the system structure holistically. ADRs are the decision log that architecture documentation can link to.
- **CHANGELOG** — tracks released changes. ADRs track decisions, not releases; the two are complementary.
