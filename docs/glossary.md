<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# Glossary — canonical terminology

The canonical terms of PortfoliFLOW (ADR-0134). Use them precisely in code,
comments, documentation, commit messages and prompts. If a task is ambiguous
about a term, ask before proceeding: calling a Section a page, or a Service a
Module, makes the code drift from the project's model, and ambiguity costs
more to debug than to clarify.

**Precedence.** An accepted ADR wins over this file. This file is canonical
for terms; `AGENTS.md` holds the rules; `docs/architecture.md` is the
narrative and carries no definitions of its own.

**Adding a term.** A term enters for something the system has built. Cite the
ADR that defines it, write one row in English, and place it in the section its
subject belongs to. A term that an accepted ADR introduces for work not yet
built enters with that build.

---

## Architecture and layers

| Term | Code mapping | Definition |
|---|---|---|
| **Area** | `module_area`, `_AREAS` (`web/shell.py`) | One of nine top-level groups, in sidebar order: Front Office, Back Office, Assistants, Planning Desk, Investor Communication, Watch Desk, Cases, Transactions, Admin (the ADR-0122 §1 order). Watch Desk was added as the sixth Area by ADR-0089; Planning Desk as the seventh by ADR-0104 §6; Cases as the eighth by ADR-0107; Transactions as the ninth by ADR-0128 §7, between Cases and Admin. ADR-0122 fixed the sidebar order above, superseding the ADR-0104 §6 order. Each has one directory under `modules/` and one URL `/{area-name}` in the web surface. |
| **Section** | `SectionMeta` in `_SECTIONS_BY_AREA` (`web/shell.py`), `data-pf-section` | One view within an Area. An Area shows exactly one Section at a time; the URL fragment selects it (e.g. `/front-office#charts`), and a Section that is not shown is not loaded. The catalogue `_SECTIONS_BY_AREA` defines which Sections an Area has, their order, the landing Section and the role gate; the Area body partial `web/templates/_partials/areas/_<area>_body.html` must match it (pinned by `tests/regression/test_section_catalogue_matches_body_partials.py`). ADR-0134 §4, superseding the long-scroll definition of ADR-0058 and ADR-0084; rules in `docs/ux/ui-standards.md` §2.1. |
| **Module** | `BaseModule` subclass, `@registry.register` | A registered unit of business logic assigned to one Area. Discoverable via `ModuleRegistry`. A user-facing module surfaces as a Section of its Area; Sections are catalogued in `web/shell.py`, not discovered from the registry. |
| **Feature** | *(planning term — not a code construct)* | A user-visible capability. May span Modules, Sections, and Functions. Use in product / roadmap discussions, not for code. |
| **Function** | Python `def` / method | A Python function or method. Nothing else. Never use "Function" to mean a Feature or a Module. |
| **Service** | Class in `services/` | An integration or calculation layer that Modules and Web routes call through defined interfaces. |
| **Repository** | Class in `core/repositories/` | Async CRUD interface for one or more ORM models. Tenant-scoped, audit-aware (ADR-0034, ADR-0041). |
| **Chart Spec** | Function under `services/chart_specs/` | A pure dict serialisable to Plotly JSON. Consumed by `web/routes/charts.py` and friends. No DB access (ADR-0045). |

## Areas and their work objects

| Term | Code mapping | Definition |
|---|---|---|
| **Planning Desk** | `modules/planning_desk/`, `/planning-desk` | The seventh Area (ADR-0104 §6). Two stacked Sections — Cash Flow Planning and Scenario Analysis — over one parameter set. It *projects and simulates* where the Watch Desk *watches and raises*: it works on the plan world (`services/investments/plan_world`) through the pure overlay contract (`services/overlay/`), and no overlay ever writes to the book. Feature #034 re-anchored here from the retired Watch Desk `scenarios` stub (ADR-0104 §8). |
| **Case** | `modules/cases/`, `/cases`, `Case` ORM | A tenant-scoped unit of decision work: an open question carried to a documented close (ADR-0107 §2). Carries an append-only timeline of entries (notes, decisions, and pins — documents, scenario snapshots, Shirley consultation excerpts) and is closed exactly once with a mandatory closing note. Opened manually or from an Irene finding via the fifth resolution `opened_case`. The eighth Area by order of introduction, sitting between the Watch Desk and Transactions in sidebar order (ADR-0107; order per ADR-0122). |
| **Trade ticket** | `modules/transactions/`, `/transactions`, `TradeTicket` ORM | One intended or recorded portfolio change carried through a single lifecycle (`draft` → `proposed` → `approved` → `booked`, or `cancelled`), settling atomically against a cash position; the rows it emits into the ledger, cashflows and NAVs are enumerated in `TradeTicketEffect` so a booking is reversible and its provenance (Watch Desk → Case → ticket → bookings) is machine-readable (ADR-0128 §1–§3, §6). The Area label is **Transactions**; the object is a trade ticket, avoiding the collision with `position_transactions`. The ninth Area, sitting between Cases and Admin in sidebar order (ADR-0128 §7). |
| **Trade ticket effect** | `trade_ticket_effects` table, `TradeTicketEffect` ORM | One row the booking of a trade ticket emitted into the ledger — a position transaction, a cashflow, a NAV or an investment update — enumerated as `(ticket_id, effect_type, effect_id)`. The ledger stays ignorant of the layer above it; the effects are the authoritative linkage, make a booked ticket reversible (cancellation deletes the enumerated effects in one transaction) and make its provenance machine-readable. ADR-0128 §2, §6. |

## Tenancy and access

| Term | Code mapping | Definition |
|---|---|---|
| **Tenant** | `Tenant` ORM row | The scoping unit of all multi-tenant data. Every domain table carries `tenant_id`, enforced by RLS (ADR-0035). |
| **Primary Tenant** | `PRIMARY_TENANT_ID` (= `SENTINEL_TENANT_ID`) | Production-name for the previously "Sentinel" tenant. Holds the Minathena Capital deployment data, subdomain `minathena-capital`. ADR-0063 §7. ADR-0063 and its sibling ADRs still carry the earlier pre-release demo identity; the row was renamed to Minathena Capital before public release, with migration `b012` edited in place as a documented exception to the immutable-migration rule. |
| **Sentinel (tenant / user)** | Created by `portfoliflow bootstrap` | The default tenant and user installed by `cli/bootstrap.py` at first deployment (ADR-0040). The Sentinel Tenant has `SENTINEL_TENANT_ID` (`core/tenant_constants.py`). Renamed to **Primary Tenant** in ADR-0063 §7; the `SENTINEL_TENANT_ID` constant is retained as a transitional alias for `PRIMARY_TENANT_ID`. |
| **System Tenant** | `SYSTEM_TENANT_ID = 00000000-0000-0000-0000-000000000000` | The platform-operations tenant. Hosts super-admin user accounts and nothing else; the schema CHECK on `users.is_super_admin` binds super-admins to this tenant. Subdomain `admin`. ADR-0063 §3, ADR-0064. |
| **Super-admin** | `users.is_super_admin = TRUE` | A user living in the System Tenant with the platform-operations role. Cannot read tenant-data from the web surface (ADR-0064 §1); emergency tenant-data reads go through `portfoliflow inspect-tenant`. ADR-0064. |
| **Tenant role** | `users.roles: TEXT[]` | One or more of `{'owner', 'member', 'auditor'}`. Owner writes domain data; Member runs analytics (including persisting results); Auditor is read-only and has tenant-scoped `audit_log` access. ADR-0063 §2. |
| **Tenant Resolver** | `services/tenant_resolution/` | Maps a request's `Host` header to a tenant id. Production implementation: `SubdomainTenantResolver` (audit-engine `tenants.subdomain` lookup). Local dev: either `*.localhost` URLs (preferred) or the `LOCAL_DEV_TENANT_SUBDOMAIN` env var. ADR-0063 §1. |
| **`LOCAL_DEV_TENANT_SUBDOMAIN`** | env var | Fallback consulted by `SubdomainTenantResolver` when the request host is bare `localhost` or `127.0.0.1` (no subdomain). Lets a developer point the dev server at one tenant without DNS or `/etc/hosts` edits. For multi-tenant dev, prefer `/etc/hosts` entries (`admin.localhost`, `minathena-capital.localhost`) — those let multiple tenants coexist in parallel browser tabs. ADR-0063 §1. |
| **`.localhost` dev subdomains** | `/etc/hosts` convention | `*.localhost` hostnames resolve to `127.0.0.1` by convention (RFC 6761); with `/etc/hosts` entries the tenant-resolver recognises `admin.localhost`, `minathena-capital.localhost`, etc. and routes each to the matching tenant. ADR-0063 §1. |

## AI, agents and tools

| Term | Code mapping | Definition |
|---|---|---|
| **Shirley** | `modules/assistants/shirley.py`, `services/ai_service_core.py` | The conversational assistant in the Assistants Area: reactive, streaming, and tool-using through the ToolRegistry. Other Areas consult her with a brief (e.g. from a Case). Distinct from Irene. ADR-0049, ADR-0051. |
| **ToolRegistry** | `services/tool_registry.py` | Single seam for AI-callable tools (ADR-0012). Every Shirley-callable tool registers a name, schema, and Trust Class. |
| **Tool Trust Class** | Enum on registered tools | One of `READ_INTERNAL`, `WRITE_INTERNAL`, `READ_EXTERNAL_UNTRUSTED`, `EXTERNAL_EFFECT`. Gates per-turn behaviour (ADR-0022). |
| **Fetcher LLM** | `send_one_shot_extraction` in `services/ai_service_core.py` | The isolated, stateless, tool-free LLM call that turns raw content from an untrusted source into a structured summary under its own system prompt. Only that summary, wrapped in `<external_content trust="untrusted">`, reaches an agent's conversation; the agent never sees the raw text. Part of the web research architecture, not a capability of Shirley. ADR-0022, ADR-0023. |
| **News Scraper vs. Report Scraper** | distinct backends | The **News Scraper** is `services/web_research/` (RSS press coverage). The **Report Scraper** is `modules/assistants/report_scraper.py` + `services/scraper/` (GP quarterly report extraction). Use the qualified term in docs and commits — never the bare word "scraper". |
| **Scoped setting** | `scoped_settings` table, `services/credential_vault/taxonomy.py` | A setting or credential held at one of three scopes — application (the environment / `.env`), tenant, or user — and resolved along a declared chain, by default `user → tenant → application`, the most specific scope holding a value winning. Every setting is either pinned to one scope or chained; a multi-field credential resolves all its fields from one scope. Edited in Admin › Providers & Credentials. ADR-0112 §1–§3, §6. |
| **Credential vault** | `services/credential_vault/`, `CREDENTIAL_VAULT_MASTER_KEY` | The encryption half of the scoped-settings architecture: secret scoped-setting rows carry a Fernet token in `value_ciphertext`, never plain text, plus at most a short hint of the value. The master key is a Fernet key supplied by the deployment; see `docs/deploy/credential-vault.md`. ADR-0112 §2. |

## Investment domain and schema

| Term | Code mapping | Definition |
|---|---|---|
| **Investment** | `Investment` ORM, `investments` table | A single tenant-scoped investment instrument. Identified by `(tenant_id, name)`. Classified by `investment_type` (one of eight canonical values) and 1:1 linked to an Asset Class. |
| **Investment Type** | `investment_type` column | One of eight canonical values: `private_equity`, `private_debt`, `real_estate`, `infra_equity`, `listed_equity`, `listed_bonds`, `cash`, `other`. `'cash'` was added as the eighth value by ADR-0100 §1 (migration `b027`) so a foreign-currency cash balance can be a first-class investment row. |
| **NAV** | `InvestmentNav` ORM | Statement-day valuation for one investment. Identified by `(investment_id, as_of_date, nav_kind)`. |
| **Cashflow** | `InvestmentCashflow` ORM | A point-in-time financial event. Multiple cashflows per investment-timestamp-type combination are allowed. |
| **`nav_kind` / `flow_kind`** | column value | `'plan'` (manager projection) or `'actual'` (realised). Plan and actual series coexist. |
| **`flow_type`** | column value | One of eight canonical cashflow-type values: `capital_call`, `distribution`, `fee`, `carry`, `dividend`, `coupon`, `other`, `investor_flow`. `'investor_flow'` was added as the eighth value by ADR-0103 §5 (migration `b028`). |
| **Investor flow** | `investment_cashflows.flow_type = 'investor_flow'` | A net contribution to, or withdrawal from, the mandate. Bookable on **cash positions only** — the investment row of the currency the flow settles in — a rule the service seam enforces (`InvestorFlowScopeError`), since it spans two tables and no CHECK can see across the FK. Signed, both `flow_kind` variants legal: `plan` flows feed the cash plan path, `actual` flows are informational (actual balances come from statement levels, so no double count). Exempt from every scenario/TA overlay transformation — see `services/investments/flow_type_invariants.py`. ADR-0103 §5. |
| **Country (ISO 3166-1 alpha-2)** | `countries` table, `iso_code` | Two-letter country code per ISO 3166-1 alpha-2; the `XX` sentinel marks unallocated splits. The `countries` table is the single global stammtabelle in the schema (ADR-0045 §2). |
| **Country split** | `investment_country_weights` | Per-investment country allocation. Weights do not need to sum to 100. |
| **Region** | `Region` ORM (`regions`), `region_country_memberships` | Coarse geographic grouping (e.g. `DACH`, `Asia Emerging`, `North America — USA`) per the M1 Strict-Partition model. Investment region splits live in `investment_region_weights`. ADR-0046. |
| **Sector** | `Sector` ORM (`sectors`) | Tenant-curated taxonomy with `(tenant_id, code)` UNIQUE. Each tenant has its own catalogue plus an `unclassified` sentinel. |
| **Sector split** | `investment_sector_weights` | Per-investment sector allocation. Same non-summation rule as country weights. |
| **AnlV Code** | `investments.anlv_code` | German Anlageverordnung classification on an investment. Joined to the global `anlv_categories` stammtabelle. ADR-0057. |
| **Limit / Limit Set** | `limits`, `limit_sets` ORM | Phase-7 investment-limit feature. Historised via `effective_from` (ADR-0056); `family IN ('saa', 'anlv')`. |
| **AUM** | `services/investments/aum.py` | `aum(t) = Σ nav_functional(t)` over **all** investments, cash rows included — a *derived* figure with one shared formulation (`compute_aum`), not a persisted series. There is no unmodelled float: what is not on a statement does not exist for the platform. The `portfolio_aum` table, model and repository were dropped by ADR-0103 §7 (migration `b030`), and the Cash residual retired with them. ADR-0103 §2. |
| **Net Capital Gain** | `services/analytics/investment_returns.py` | Cumulative distributions − cumulative calls + NAV, computed as a time series. The orange "ncg" line in the Cashflows tile. |
| **Total Return since Inception** | `services/analytics/investment_returns.py` | Cumulative-product return index `(1 + r_t).cumprod() * 100`. Indexed to 100 at inception. |
| **Six-tile review** | `modules/investor_communication/portfolio_review.py`, `services/portfolio_review/` | The Portfolio Review report layout: 3×2 grid of charts plus a header KPI strip. |

## Currency and cash

| Term | Code mapping | Definition |
|---|---|---|
| **Functional currency** | `tenants.functional_currency` | The portfolio's reporting currency, set per tenant. Every aggregate the converted seams publish is stated in it: the review and limits seams (ADR-0099 §4) and — since ADR-0102 — the **statistics**, **portfolio-analysis / SAA**, and **benchmark** sections, which measure returns in it too (FX effect included). ADR-0099 §Context, §2; ADR-0102 §1. |
| **Position currency** | `investments.currency`, per-series `currency` columns | The currency an investment is denominated and settled in. Distinct from the functional currency; conversion between the two happens at the ADR-0099 §4 boundary. ADR-0099 §Context. |
| **Reference currency** | base of the `fx_rates` dataset | The currency the FX-rate dataset is quoted against — a property of the *data*, not of the portfolio. It may coincide with the functional currency, but the two are distinct concepts and must not be conflated. ADR-0099 §2. |
| **Explicit cash position** | `investments.investment_type = 'cash'` | A cash balance held in a non-functional currency, modelled as an ordinary investment row (migration `b027`). Converted, limit-checked and AnlV-classifiable through the existing machinery. ADR-0100 §2. |
| **Cash residual** | *(retired)* | The ADR-0055 formula `aum_total − Σ nav_functional` for the uninvested remainder. **Retired by ADR-0103 §2** together with the `portfolio_aum` series it subtracted from: all cash is now an **Explicit cash position**, so the float is modelled rather than inferred, and a denominator derived from the NAVs cannot go stale against them. The negative-residual suppression rule (ADR-0055/0067) retired with it. Do not reintroduce the term. |

## Watch Desk

| Term | Code mapping | Definition |
|---|---|---|
| **Irene** | `services/irene/`, `irene_*` tables | The Watch Desk's proactive agent: a scheduled, non-streaming producer that turns internal portfolio state and external press signals into findings on a slow cadence. Distinct from Shirley, who is reactive and conversational. ADR-0085; ADR-0115 renamed the Area to Watch Desk and kept the agent's name. |
| **Beat** | `services/irene/beat.py`, `irene_schedule` | One run of Irene for one tenant. A tenant-blind tick asks which tenants are due (`next_due_at`, per-tenant cadence); each due tenant gets a beat, which observes the watched world, compares it with the stored prior and surfaces findings. Runs outside the web process and never runs twice at once for a tenant. ADR-0086. |
| **Finding** | `irene_finding` table, `IreneFinding` ORM | A decision-support artefact Irene surfaces on a rising edge. Append-only, with a lifecycle: born `open`, then resolved once as `acted`, `dismissed`, `acknowledged` or `opened_case` (handed over to a Case). ADR-0085, ADR-0107. |
| **Watchpoint** | `watchpoints` ORM, `WatchpointRepository` | What the Watch Desk observes and at which threshold, per tenant. Historised like `limit_sets`: a stable `watchpoint_id` with immutable version rows keyed `effective_from`, and retirement is a version (`retired = true`), never a delete. **Two shapes, one table** (ADR-0116 §3): for `saa` / `anlv` / `rss` a watchpoint is a *sensitivity overlay only* (mute, WARN override, re-trigger Δ — `rss`: mute alone), because the subject and its ceiling belong to the limit set; for the four signal families the watchpoint **defines** the subject. `freshness` and `liquidity` are singletons — one live identity per tenant. ADR-0116 §1. |
| **Signal family** | `services/analytics/{price_watch,fx_watch,nav_freshness,cash_coverage_watch}.py` | One of the four defined families `price` / `fx` / `freshness` / `liquidity` (ADR-0116 §4), each with a pure producer over the `signal_watch` contract. Magnitudes are stated in **badness units** (larger is always worse) so ADR-0087's edge arithmetic is reused unchanged. Statuses stay `OK`/`WARN`/`BREACH` internally but render **Calm / Approaching / Triggered** — never "breach", which is regulatory language reserved for the quota families. As implemented, `freshness` measures NAV age against `max_age_days` and `liquidity` measures percent-of-the-way-to-the-floor on a fixed 100-point scale; both depart from the ADR's magnitude cells, and the record is roadmap **#057**, not a successor ADR. `liquidity` renders **ratios** only — the 100-scale never reaches a string. |
| **Watch Desk resolution** | `services/watch_desk/overlay.py`, `signal_observation.py` | The one per-tenant answer to "what is watched, at which thresholds" (`resolve_watch_desk`) and the one per-family fetch-and-produce path beneath it (`observe_signal_families`, read-only). The beat and the monitor share **both**, so a monitor row is the number the next beat will classify rather than a second computation; the monitor never reads a status from `irene_watch_state` and never writes. Pinned structurally by `tests/regression/test_watch_desk_single_resolution.py`. ADR-0116 §1, §6. |

## Provider channel

| Term | Code mapping | Definition |
|---|---|---|
| **Provider channel** | `services/provider_channel/`, `provider_channel.enabled` | The opt-in path by which a tenant picks a provider from the suggestion list and sends it an end-to-end-encrypted order or engagement message, receiving a structured confirmation that pre-fills the booking of a trade ticket. Off by default (ADR-0131). The repository holds the client side — the message and confirmation contract, the directory format and its signature check, the sealed export; the relay and the provider portal are a separately operated service. ADR-0129, ADR-0108. |
| **Suggestion list** (provider directory) | `services/provider_directory/`, `services/provider_channel/directory.py` | The signed, versioned directory of providers — display data, provider types, supported ticket kinds and engagement categories, coverage hints, one public encryption key per provider. The instance fetches it only while the channel is enabled, verifies the signature before trusting any key, keeps a local copy with its provenance, and filters it locally, so the directory service never learns the portfolio. ADR-0129 §2. |
| **Engagement** | `MESSAGE_TYPE_ENGAGEMENT`, `ENGAGEMENT_CATEGORIES` (`services/provider_channel/`) | An advisory, legal, fund-selection or second-opinion request carried on the provider channel — the sibling of an order, with no book effect. As built, the term exists in the channel contract (a message type and the directory's engagement categories); the tenant-scoped engagement object is not built yet. ADR-0129 §5. |

## Project name spellings

| Spelling | Usage |
|---|---|
| `portfoliflow` | **Canonical.** Package name, console scripts (`portfoliflow`, `portfoliflow-web`), Postgres role (`portfoliflow_app`), logger hierarchies, file paths (`~/.portfoliflow/`), Python identifier (`PortfoliFlowError`). |
| `PortfoliFLOW` | Brand name in prose — docstrings, ADR body text, README, `APP_NAME`, user-facing strings. |
| `portfolioflow` | **Obsolete.** Earlier misspelling with a redundant second "o". Must not be used in new code. See ADR-0044. |

## Terminology mistakes to avoid

| Don't say | Say | Why |
|---|---|---|
| page, for a view in an Area | **Section** | One Section per view (ADR-0134 §4). |
| Module for a Service, or Service for a Module | **Module** for a unit under `modules/`, **Service** for a layer under `services/` | Two different layers with different dependency rules. |
| Function for a Feature or a Module | **Feature** or **Module** | A Function is a Python `def`, nothing else. |
| scraper (bare) | **News Scraper** or **Report Scraper** | Two distinct backends. |
| breach, for a signal family | **Triggered** (Calm / Approaching / Triggered) | "Breach" is regulatory language reserved for the quota families (ADR-0116). |
| transaction, for the object the Transactions Area works on | **trade ticket** | Avoids the collision with the `position_transactions` ledger (ADR-0128). |
| Decision Console | **Watch Desk** | Renamed by ADR-0115; older ADRs keep the old name. |
| Widget, Panel, as structural terms | **Section**, **Area** | Legacy Qt terms, retired with the Qt surface (ADR-0084, ADR-0094). `pf-panel` is a UI component, not a structural term. |
| Cash residual, portfolio AUM series | **Explicit cash position**; **AUM** as a derived figure | Retired by ADR-0103. |
| V2, V2 format | **Excel import format**, **Excel import file** | ADR-0059; `format_version = "v2"` survives only as a persisted identifier. |
| Sentinel Tenant, in new prose | **Primary Tenant** | Renamed by ADR-0063 §7; `SENTINEL_TENANT_ID` is a transitional alias. |

## German-to-English equivalents

English is the only language of code, comments and documentation (ADR-0008);
German survives only as data values, such as labels in an Excel import file.
These are the canonical English terms for German domain terms that appeared
in the codebase before the language clean-up.

| German | English (canonical) | Notes |
|---|---|---|
| Bereich | Area | A top-level grouping of modules. |
| Stichtag | as-of date | The reference date of a snapshot. |
| Investiertes Kapital | invested capital | |
| Vintage / Vintage-Jahr | vintage / vintage year | Already borrowed from English; keep. |
| Kapitalabruf | capital call | `flow_type = 'capital_call'`. |
| Ausschüttung | distribution | `flow_type = 'distribution'`. |

### UI strings

| German | English (canonical) |
|---|---|
| Trotzdem importieren | Import anyway |
| Abbrechen | Cancel |
| Hochladen | Upload |
| Speichern | Save |
