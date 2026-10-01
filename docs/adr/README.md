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
| 0032 | [UI Theme Schema Extension for Layout, Pill, and Font Tokens](./0032-ui-theme-schema-extension-layout-pill-font.md) | Deprecated (2026-09-30) — its subject, the PyQt6 widget code, was removed by ADR-0094; the web `--ui-*` token layer covers layout and typography | 2026-04-29 | ui, architecture, process |
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
| 0131 | [Provider Channel Opt-In Switch in the Scoped-Settings Taxonomy — `provider_channel.enabled`, Tenant-Scoped, Config-Only](./0131-provider-channel-enabled-in-the-scoped-settings-taxonomy.md) | Accepted (2026-09-11) — annex amendment to ADR-0112 §3 (third, after 0118/0123): config-only provider `provider_channel` with the tenant's opt-in switch `enabled`; `env_fallback=False` (no deployment-wide phone-home switch), `optional=True`; Admin card copy states what leaves the instance (ADR-0129 §6); when off, Transactions shows no gesture (Stage B record B-D-25). | 2026-09-11 | provider-channel, configuration, multi-tenancy, credentials, admin, privacy |
| 0132 | [Web Research Model in the Scoped-Settings Taxonomy — Per-Call, Per-Tenant Resolution for the News Path](./0132-web-research-model-in-the-scoped-settings-taxonomy.md) | Accepted (2026-09-28) — annex amendment to ADR-0112 §3 (fourth, after 0118/0123/0131) and **amendment to ADR-0047**: one new `openrouter` config field, `research_model` (tenant-only, env link `RESEARCH_MODEL`, label "Web research model"), resolved scope-major research-first per **tool call** inside the turn's `tenant_context` and never stashed; `ToolExecutionContext` gains `user_id` so the user scope is reachable from a tool; one model serves both news LLMs; `_tool_session` lifted to its own module; `web/main.py::_configure_ai_core` deleted — the singleton's last web consumer is gone and `.env` is the application scope only. | 2026-09-28 | web-research, configuration, multi-tenancy, credentials, admin, openrouter, tools |
| 0133 | [Watch Desk Briefing Artefact and Watch-Point Scope — Second Synthesis Step, Family Switches, Sensitivity Presets](./0133-watch-desk-briefing-artefact-and-watch-point-scope.md) | Accepted (2026-09-30) — annex amendment to ADR-0088 (a second synthesis call after the deterministic floor writes a persisted, append-only briefing per beat) and to ADR-0116 §1/§3/§5/§7 (historised, audited family on/off scope beside the watchpoints; per-family WARN step and presets Quiet · Standard · Nervous; owner-only editing); extends the ADR-0107 C6 consultation brief with a briefing kind; corrects `get_system_prompt` so Irene is never presented as Shirley; migration `b036`. | 2026-09-30 | watch-desk, irene, shirley, synthesis, briefing, watchpoints, calibration, audit, llm, prompts, ux |
| 0134 | [AGENTS.md as the Agent Instruction File, a Standalone Canonical Glossary, and Section as One View](./0134-agents-md-standalone-glossary-and-section-as-one-view.md) | Accepted (2026-09-30) — **amends ADR-0015** (the project instruction file is `AGENTS.md`; no `CLAUDE.md` in the tree); **supersedes ADR-0084 in part** (glossary location — `docs/glossary.md`, the only copy — item 5 precedence, and the Section sentence of item 1); **supersedes ADR-0058 in part** (long-scroll sections and the section indicator: one Section per view, selected by the URL fragment, catalogued in `web/shell.py`); precedence ADR › `AGENTS.md` (rules) › `docs/glossary.md` (terms) › `docs/architecture.md` (narrative). | 2026-09-30 | process, glossary, documentation, ai-workflow, navigation, ui |

The next free ADR number is **0135**.

## Reading Older ADRs

- **Instruction file and glossary.** Older ADRs refer to the agent instruction file by its former name, `CLAUDE.md`, and to the glossary as living in it. Read those references as `AGENTS.md` for rules and `docs/glossary.md` for terms (ADR-0134). The ADRs themselves are not edited.
- **Roadmap identifiers.** ADRs written before June 2026 cite roadmap items by legacy IDs (`A1`, `B2`, `B5d`, …). The appendix of [`docs/roadmap.md`](../roadmap.md) maps them to the flat `#NNN` IDs.
- **Renumbering.** On 2026-06-03 the file formerly at `0069-single-investment-review-web-surface.md` was renumbered to **0073**; `0069-shirley-back-office-analysis-tools.md` keeps 0069. ADR-0058 was renumbered from a draft 0046, as its revision history records.
- **Documents not published with the repository.** Some ADRs cite internal working documents — audit notes, handover material and mockups, reports — under `docs/_archive/`, `docs/_audit/`, `docs/handover/` and `docs/reports/`. They are not part of the public repository; the ADR carries the decision, and the cited material was its working evidence.
- **Historical phase documents.** `0000-retrofit-report.md` and the phase-3 and phase-4 documents under `docs/` are kept because accepted ADRs cite them. They record the state at the time and are not current guidance.

Deferred and planned work is tracked in [`docs/roadmap.md`](../roadmap.md).

## Relation to Other Documentation

- **AGENTS.md** — the rules coding agents follow when working on the codebase (ADR-0134). Cross-reference relevant ADRs from there. Terms are defined in `docs/glossary.md`.
- **Docstrings** — document *what* a function or class does and how to use it. Cross-reference ADRs from docstrings where a non-obvious design choice is encoded in the code.
- **arc42 / architecture documentation** (if introduced) — describes the system structure holistically. ADRs are the decision log that architecture documentation can link to.
- **[`CHANGELOG.md`](../../CHANGELOG.md)** — tracks released changes. ADRs track decisions, not releases; the two are complementary.
