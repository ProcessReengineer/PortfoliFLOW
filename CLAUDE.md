# PortfoliFLOW — Instructions for AI Assistants

This file is read automatically by Claude Code at the start of every
session. It contains the conventions and rules that govern all code
generation for this project. Follow these instructions precisely.

This file is *runtime context*, not onboarding documentation. For
the full architecture see `docs/architecture.md`. For decisions and
their rationale see `docs/adr/`. For planned work see `docs/roadmap.md`.

---

## What this project is

PortfoliFLOW is an AI-assisted portfolio management platform for
institutional investors. It serves institutional portfolio
management broadly — including, but not limited to, fund-of-funds
and alternative-investment mandates — and automates nine operational
areas — front office, back office, assistants, planning desk,
investor communication, watch desk, cases, transactions, and admin —
through a web application backed by Postgres with row-level security
per tenant.

The project is maintained by a single developer with AI assistance.
Every AI-generated change must be reviewable by a human in a single
focused session — no sprawling diffs, no surprise modifications to
files outside the stated scope.

---

## Architecture in one paragraph

The primary surface is a FastAPI/Jinja2/HTMX web application under
`web/`, served against Postgres with row-level security per tenant
(ADR-0034, ADR-0035) and session-based authentication (ADR-0036).
ORM models live in `core/models/`; tenant-scoped data access goes
through `core/repositories/`. Business modules live under `modules/`,
organised into the nine operational areas (ADR-0089, ADR-0104, ADR-0107, ADR-0128). The `services/` layer
provides pure, DB-free and Qt-free calculation engines under
`services/analytics/`, Plotly chart specifications under
`services/chart_specs/`, the AI service core (`services/ai_service_core.py`),
the Tool Registry (`services/tool_registry.py`), and integration
backends (web research, report scraper, and the provider-agnostic
market-data import under `services/market_data/` — a second ingest
producer, provider-blind behind a port + normalised DTO, ADR-0091).
`services/fx/` is the pure FX conversion service (identity
short-circuit, triangulation, carry-forward, typed
`MissingFxRateError` — never a silent 1:1 fallback); it sits at the
single conversion boundary in front of the analytics layer (ADR-0099
§4), so analytics keeps the single-currency contract and its ADR-0013
purity. The optional Telegram bot
under `bot/` is a sibling consumer of the AI service.

The legacy PyQt6 surface under `gui/` was removed in July 2026
(ADR-0094 Stage 1) — see §Legacy below.

For full details read `docs/architecture.md`.

---

## Dependency rules — hard constraints

```
web/    ──► modules/   ──► services/  ──► core/
                       └─► core/repositories/  ──► core/models/
bot/    ──► services/ai_service_core
services/analytics/      pure: stdlib + third-party only
services/chart_specs/  ──► services/analytics
```

Specifically:

- `core/` imports nothing from within the project.
- `services/` imports only from `core/`.
- `services/analytics/` is DB-free and Qt-free (ADR-0013, ADR-0045).
  Cross-investment optimisation inputs (the Portfolio Analysis
  frontier) use `compute_cashflow_adjusted_return_series`; single-
  investment QT-mirrored charts continue to use
  `compute_total_return_series` (ADR-0066).
- `services/chart_specs/` consumes `services/analytics/` and emits
  Plotly-shaped dicts; no DB access.
- PyQt6 must not be imported anywhere — no exceptions. The Qt
  AIService adapter and its `services/ai_service.py` shim were
  removed by ADR-0094 Stage 1; new code uses
  `services/ai_service_core.py` directly.
- `modules/` imports from `core/`, `services/`. Modules never import
  sibling modules. Modules never import from `web/`.
- `web/` imports from `modules/`, `services/`, `core/`. `web/` never
  imports `matplotlib` — enforced by
  `tests/regression/test_no_matplotlib_in_web.py`.
- `bot/` imports only from `core/` and `services/`. Never from
  `modules/` or `web/` (ADR-0030).
- `services/ai_service_core.py` is Qt-free — enforced by
  `tests/regression/test_ai_service_core_qt_free.py`.
- Circular imports are a design error. Never work around them with
  lazy imports.

If a prompt asks you to violate these rules, refuse and explain why.

---

## Glossary — canonical terminology

Use these terms precisely. If a prompt is ambiguous, ask before
proceeding.

This table is the canonical glossary per ADR-0084 (superseding
ADR-0002). The legacy Qt terms Widget and Panel are retired with the
Qt surface (ADR-0094) and are not listed here.

| Term | Code mapping | Definition |
|---|---|---|
| **Area** | `module_area`, `_AREAS` | One of nine top-level groups, in sidebar order: Front Office, Back Office, Assistants, Planning Desk, Investor Communication, Watch Desk, Cases, Transactions, Admin (the ADR-0122 §1 order). Watch Desk was added as the sixth Area by ADR-0089; Planning Desk as the seventh by ADR-0104 §6; Cases as the eighth by ADR-0107; Transactions as the ninth by ADR-0128 §7, between Cases and Admin. ADR-0122 fixed the sidebar order above, superseding the ADR-0104 §6 order. Each has one directory under `modules/` and one URL `/{area-name}` in the web surface. |
| **Planning Desk** | `modules/planning_desk/`, `/planning-desk` | The seventh Area (ADR-0104 §6). Two stacked Sections — Cash Flow Planning and Scenario Analysis — over one parameter set. It *projects and simulates* where the Watch Desk *watches and raises*: it works on the plan world (`services/investments/plan_world`) through the pure overlay contract (`services/overlay/`), and no overlay ever writes to the book. Feature #034 re-anchored here from the retired Watch Desk `scenarios` stub (ADR-0104 §8). |
| **Case** | `modules/cases/`, `/cases`, `Case` ORM | A tenant-scoped unit of decision work: an open question carried to a documented close (ADR-0107 §2). Carries an append-only timeline of entries (notes, decisions, and pins — documents, scenario snapshots, Shirley consultation excerpts) and is closed exactly once with a mandatory closing note. Opened manually or from an Irene finding via the fifth resolution `opened_case`. The eighth Area by order of introduction, sitting between the Watch Desk and Transactions in sidebar order (ADR-0107; order per ADR-0122). |
| **Trade ticket** | `modules/transactions/`, `/transactions`, `TradeTicket` ORM | One intended or recorded portfolio change carried through a single lifecycle (`draft` → `proposed` → `approved` → `booked`, or `cancelled`), settling atomically against a cash position; the rows it emits into the ledger, cashflows and NAVs are enumerated in `TradeTicketEffect` so a booking is reversible and its provenance (Watch Desk → Case → ticket → bookings) is machine-readable (ADR-0128 §1–§3, §6). The Area label is **Transactions**; the object is a trade ticket, avoiding the collision with `position_transactions`. The ninth Area, sitting between Cases and Admin in sidebar order (ADR-0128 §7). |
| **Section** | Long-scroll subdivision in a web area | A section within an Area's long-scroll page, addressable via anchor (e.g. `/front-office#charts`). Multiple sections per area. Per ADR-0058. |
| **Module** | `BaseModule` subclass, `@registry.register` | A registered unit of business logic assigned to one Area. Discoverable via `ModuleRegistry`. Each module renders into a Section in its Area's web page. |
| **Feature** | *(planning term — not a code construct)* | A user-visible capability. May span Modules, Sections, and Functions. Use in product / roadmap discussions, not for code. |
| **Function** | Python `def` / method | A Python function or method. Nothing else. Never use "Function" to mean a Feature or a Module. |
| **Service** | Class in `services/` | An integration or calculation layer that Modules and Web routes call through defined interfaces. |
| **Repository** | Class in `core/repositories/` | Async CRUD interface for one or more ORM models. Tenant-scoped, audit-aware (ADR-0034, ADR-0041). |
| **Tenant** | `Tenant` ORM row | The scoping unit of all multi-tenant data. Every domain table carries `tenant_id`, enforced by RLS (ADR-0035). |
| **Sentinel (tenant / user)** | Created by `portfoliflow bootstrap` | The default tenant and user installed by `cli/bootstrap.py` at first deployment (ADR-0040). The Sentinel Tenant has `SENTINEL_TENANT_ID`. Renamed to **Primary Tenant** in ADR-0063 §7; the `SENTINEL_TENANT_ID` constant is retained as a transitional alias for `PRIMARY_TENANT_ID`. |
| **Primary Tenant** | `PRIMARY_TENANT_ID` (= `SENTINEL_TENANT_ID`) | Production-name for the previously "Sentinel" tenant. Holds the Minathena Capital deployment data, subdomain `minathena-capital`. ADR-0063 §7. ADR-0063 and its sibling ADRs still carry the earlier pre-release demo identity; the row was renamed to Minathena Capital before public release, with migration `b012` edited in place as a documented exception to the immutable-migration rule. |
| **System Tenant** | `SYSTEM_TENANT_ID = 00000000-0000-0000-0000-000000000000` | The platform-operations tenant. Hosts super-admin user accounts and nothing else; the schema CHECK on `users.is_super_admin` binds super-admins to this tenant. Subdomain `admin`. ADR-0063 §3, ADR-0064. |
| **Tenant Resolver** | `services/tenant_resolution/` | Maps a request's `Host` header to a tenant id. Production implementation: `SubdomainTenantResolver` (audit-engine `tenants.subdomain` lookup). Local dev: either `*.localhost` URLs (preferred) or the `LOCAL_DEV_TENANT_SUBDOMAIN` env var. ADR-0063 §1. |
| **`LOCAL_DEV_TENANT_SUBDOMAIN`** | env var | Fallback consulted by `SubdomainTenantResolver` when the request host is bare `localhost` or `127.0.0.1` (no subdomain). Lets a developer point the dev server at one tenant without DNS or `/etc/hosts` edits. For multi-tenant dev, prefer `/etc/hosts` entries (`admin.localhost`, `minathena-capital.localhost`) — those let multiple tenants coexist in parallel browser tabs. ADR-0063 §1. |
| **`.localhost` dev subdomains** | `/etc/hosts` convention | `*.localhost` hostnames resolve to `127.0.0.1` by convention (RFC 6761); with `/etc/hosts` entries the tenant-resolver recognises `admin.localhost`, `minathena-capital.localhost`, etc. and routes each to the matching tenant. ADR-0063 §1. |
| **Super-admin** | `users.is_super_admin = TRUE` | A user living in the System Tenant with the platform-operations role. Cannot read tenant-data from the web surface (ADR-0064 §1); emergency tenant-data reads go through `portfoliflow inspect-tenant`. ADR-0064. |
| **Tenant role** | `users.roles: TEXT[]` | One or more of `{'owner', 'member', 'auditor'}`. Owner writes domain data; Member runs analytics (including persisting results); Auditor is read-only and has tenant-scoped `audit_log` access. ADR-0063 §2. |
| **ToolRegistry** | `services/tool_registry.py` | Single seam for AI-callable tools (ADR-0012). Every Shirley-callable tool registers a name, schema, and Trust Class. |
| **Tool Trust Class** | Enum on registered tools | One of `READ_INTERNAL`, `WRITE_INTERNAL`, `READ_EXTERNAL_UNTRUSTED`, `EXTERNAL_EFFECT`. Gates per-turn behaviour (ADR-0022). |
| **Chart Spec** | Function under `services/chart_specs/` | A pure dict serialisable to Plotly JSON. Consumed by `web/routes/charts.py` and friends. No DB access (ADR-0045). |
| **Investment** | `Investment` ORM model, `investments` table | A single tenant-scoped investment instrument. Identified by `(tenant_id, name)`. Classified by `investment_type` (one of eight canonical values) and 1:1 linked to an Asset Class. |
| **Investment Type** | `investment_type` column | One of eight canonical values: `private_equity`, `private_debt`, `real_estate`, `infra_equity`, `listed_equity`, `listed_bonds`, `cash`, `other`. `'cash'` was added as the eighth value by ADR-0100 §1 (migration `b027`) so a foreign-currency cash balance can be a first-class investment row. |
| **NAV** | `InvestmentNav` ORM | Statement-day valuation for one investment. Identified by `(investment_id, as_of_date, nav_kind)`. |
| **Cashflow** | `InvestmentCashflow` ORM | A point-in-time financial event. Multiple cashflows per investment-timestamp-type combination are allowed. |
| **`nav_kind` / `flow_kind`** | column value | `'plan'` (manager projection) or `'actual'` (realised). Plan and actual series coexist. |
| **`flow_type`** | column value | One of eight canonical cashflow-type values: `capital_call`, `distribution`, `fee`, `carry`, `dividend`, `coupon`, `other`, `investor_flow`. `'investor_flow'` was added as the eighth value by ADR-0103 §5 (migration `b028`). |
| **Investor flow** | `investment_cashflows.flow_type = 'investor_flow'` | A net contribution to, or withdrawal from, the mandate. Bookable on **cash positions only** — the investment row of the currency the flow settles in — a rule the service seam enforces (`InvestorFlowScopeError`), since it spans two tables and no CHECK can see across the FK. Signed, both `flow_kind` variants legal: `plan` flows feed the cash plan path, `actual` flows are informational (actual balances come from statement levels, so no double count). Exempt from every scenario/TA overlay transformation — see `services/investments/flow_type_invariants.py`. ADR-0103 §5. |
| **Country (ISO 3166-1 alpha-2)** | `countries` table, `iso_code` | Two-letter country code per ISO 3166-1 alpha-2; the `XX` sentinel marks unallocated splits. The `countries` table is the single global stammtabelle in the schema (ADR-0045 §2). |
| **Country split** | `investment_country_weights` | Per-investment country allocation. Weights do not need to sum to 100. |
| **Region** | `regions` ORM, `region_country_memberships` | Coarse geographic grouping (e.g. `DACH`, `Asia Emerging`, `North America — USA`) per the M1 Strict-Partition model. Investment region splits live in `investment_region_weights`. ADR-0046. |
| **Sector** | `sectors` ORM | Tenant-curated taxonomy with `(tenant_id, code)` UNIQUE. Each tenant has its own catalogue plus an `unclassified` sentinel. |
| **Sector split** | `investment_sector_weights` | Per-investment sector allocation. Same non-summation rule as country weights. |
| **AnlV Code** | `investments.anlv_code` | German Anlageverordnung classification on an investment. Joined to the global `anlv_categories` stammtabelle. ADR-0057. |
| **Limit / Limit Set** | `limits`, `limit_sets` ORM | Phase-7 investment-limit feature. Historised via `effective_from` (ADR-0056); `family IN ('saa', 'anlv')`. |
| **Watchpoint** | `watchpoints` ORM, `WatchpointRepository` | What the Watch Desk observes and at which threshold, per tenant. Historised like `limit_sets`: a stable `watchpoint_id` with immutable version rows keyed `effective_from`, and retirement is a version (`retired = true`), never a delete. **Two shapes, one table** (ADR-0116 §3): for `saa` / `anlv` / `rss` a watchpoint is a *sensitivity overlay only* (mute, WARN override, re-trigger Δ — `rss`: mute alone), because the subject and its ceiling belong to the limit set; for the four signal families the watchpoint **defines** the subject. `freshness` and `liquidity` are singletons — one live identity per tenant. ADR-0116 §1. |
| **Signal family** | `services/analytics/{price_watch,fx_watch,nav_freshness,cash_coverage_watch}.py` | One of the four defined families `price` / `fx` / `freshness` / `liquidity` (ADR-0116 §4), each with a pure producer over the `signal_watch` contract. Magnitudes are stated in **badness units** (larger is always worse) so ADR-0087's edge arithmetic is reused unchanged. Statuses stay `OK`/`WARN`/`BREACH` internally but render **Calm / Approaching / Triggered** — never "breach", which is regulatory language reserved for the quota families. As implemented, `freshness` measures NAV age against `max_age_days` and `liquidity` measures percent-of-the-way-to-the-floor on a fixed 100-point scale; both depart from the ADR's magnitude cells, and the record is roadmap **#057**, not a successor ADR. `liquidity` renders **ratios** only — the 100-scale never reaches a string. |
| **Watch Desk resolution** | `services/watch_desk/overlay.py`, `signal_observation.py` | The one per-tenant answer to "what is watched, at which thresholds" (`resolve_watch_desk`) and the one per-family fetch-and-produce path beneath it (`observe_signal_families`, read-only). The beat and the monitor share **both**, so a monitor row is the number the next beat will classify rather than a second computation; the monitor never reads a status from `irene_watch_state` and never writes. Pinned structurally by `tests/regression/test_watch_desk_single_resolution.py`. ADR-0116 §1, §6. |
| **AUM** | `services/investments/aum.py` | `aum(t) = Σ nav_functional(t)` over **all** investments, cash rows included — a *derived* figure with one shared formulation (`compute_aum`), not a persisted series. There is no unmodelled float: what is not on a statement does not exist for the platform. The `portfolio_aum` table, model and repository were dropped by ADR-0103 §7 (migration `b030`), and the Cash residual retired with them. ADR-0103 §2. |
| **Functional currency** | `tenants.functional_currency` | The portfolio's reporting currency, set per tenant. Every aggregate the converted seams publish is stated in it: the review and limits seams (ADR-0099 §4) and — since ADR-0102 — the **statistics**, **portfolio-analysis / SAA**, and **benchmark** sections, which measure returns in it too (FX effect included). ADR-0099 §Context, §2; ADR-0102 §1. |
| **Position currency** | `investments.currency`, per-series `currency` columns | The currency an investment is denominated and settled in. Distinct from the functional currency; conversion between the two happens at the ADR-0099 §4 boundary. ADR-0099 §Context. |
| **Reference currency** | base of the `fx_rates` dataset | The currency the FX-rate dataset is quoted against — a property of the *data*, not of the portfolio. It may coincide with the functional currency, but the two are distinct concepts and must not be conflated. ADR-0099 §2. |
| **Explicit cash position** | `investments.investment_type = 'cash'` | A cash balance held in a non-functional currency, modelled as an ordinary investment row (migration `b027`). Converted, limit-checked and AnlV-classifiable through the existing machinery. ADR-0100 §2. |
| **Cash residual** | *(retired)* | The ADR-0055 formula `aum_total − Σ nav_functional` for the uninvested remainder. **Retired by ADR-0103 §2** together with the `portfolio_aum` series it subtracted from: all cash is now an **Explicit cash position**, so the float is modelled rather than inferred, and a denominator derived from the NAVs cannot go stale against them. The negative-residual suppression rule (ADR-0055/0067) retired with it. Do not reintroduce the term. |
| **Net Capital Gain** | `services/analytics/investment_returns.py` | Cumulative distributions − cumulative calls + NAV, computed as a time series. The orange "ncg" line in the Cashflows tile. |
| **Total Return since Inception** | `services/analytics/investment_returns.py` | Cumulative-product return index `(1 + r_t).cumprod() * 100`. Indexed to 100 at inception. |
| **Six-tile review** | `modules/investor_communication/portfolio_review.py` | The Portfolio Review report layout: 3×2 grid of charts plus a header KPI strip. |
| **News Scraper vs. Report Scraper** | distinct backends | The **News Scraper** is `services/web_research/` (RSS press coverage). The **Report Scraper** is `modules/assistants/report_scraper.py` + `services/scraper/` (GP quarterly report extraction). Use the qualified term in docs and commits — never the bare word "scraper". |

### Project name spellings

| Spelling | Usage |
|---|---|
| `portfoliflow` | **Canonical.** Package name, console scripts (`portfoliflow`, `portfoliflow-web`), Postgres role (`portfoliflow_app`), logger hierarchies, file paths (`~/.portfoliflow/`), Python identifier (`PortfoliFlowError`). |
| `PortfoliFLOW` | Brand name in prose — docstrings, ADR body text, README, `APP_NAME`, user-facing strings. |
| `portfolioflow` | **Obsolete.** Earlier misspelling with a redundant second "o". Must not be used in new code. See ADR-0044. |

---

## The one rule that matters most

Use the glossary terms precisely. If your description deviates from
canonical vocabulary (e.g. calling a `Section` a `Page`, or a
`Service` a `Module`), the resulting code drifts from the project's
mental model. Ambiguity is more expensive to debug than to clarify.

### Common mistakes to avoid

| Don't | Do |
|---|---|
| Define business logic in `web/routes/*.py` | Put it in `modules/` or `services/`. Routes are thin glue. |
| Bypass repositories to query the ORM directly | Use the repository for the relevant model in `core/repositories/`. |
| Import `matplotlib` in `web/` | Charts in `web/` go through `services/chart_specs/` → Plotly. |
| Instantiate an LLM client directly | Use `get_ai_service_core()`. |
| Add an AI-callable tool without the ToolRegistry | Register it via `get_tool_registry()` with a Trust Class. |
| Cross-import sibling modules under `modules/` | Push shared code down to `services/`. |
| Mutate a `services/analytics/` function to read from the DB | Keep analytics pure. Pass the data as arguments. |

---

## Persistence

Persistence is Postgres with multi-tenant row-level security
(ADR-0034, ADR-0035). Read and write access goes through async
repositories in `core/repositories/`. RLS evaluates per-row, scoped
by the request's tenant context (ADR-0035, ADR-0047).

The legacy `core.data_store.DataStore` in-memory singleton no longer
backs any UI — the Qt surface was removed in ADR-0094 Stage 1. It
survives in the codebase under the Strangler coexistence (ADR-0041),
now consumed only by the DataStore-coupled reporting engine and a few
module shells, and is staged for removal in Qt-sunset Stage 2
(ADR-0094 §5, roadmap #035). Web code, bot code, and modules called
from web context must use repositories — never the in-memory data
store.

Database migrations live under `db/migrations/versions/` and use
Alembic. Migration files are named
`YYYY_MM_DD_HHMM_bNNN_<descriptor>.py` (the latest at time of
writing is
`2026_08_10_1200_b033_add_watchpoints.py`).

---

## Valid module areas

A module's `module_area` must be one of:

- `front_office`
- `back_office`
- `assistants`
- `planning_desk`
- `investor_communication`
- `watch_desk`
- `cases`
- `transactions`
- `admin`

These are exactly the nine top-level Areas, listed in sidebar order
(ADR-0122 §1).
Adding a new top-level Area is an architectural decision and requires
an ADR — the precedents are Watch Desk, added as the sixth Area
by ADR-0089, Planning Desk, added as the seventh by ADR-0104 §6,
Cases, added as the eighth by ADR-0107, and Transactions, added as
the ninth by ADR-0128 §7.
Adding a new Module to an existing Area does not.

---

## Multi-tenant local development

PortfoliFLOW resolves tenants by subdomain (ADR-0063 §1). For local
development, add these entries to `/etc/hosts` (one-time setup):

    127.0.0.1   admin.localhost
    127.0.0.1   minathena-capital.localhost

After running `./scripts/db-reset.sh` and `portfoliflow-web`:

- Log in as super-admin: <http://admin.localhost:8000/login>
- Log in as primary owner: <http://minathena-capital.localhost:8000/login>

Both can be open in parallel browser tabs; sessions are independent
(different subdomains, different session cookies).

To add a new tenant subdomain for testing (e.g. `vwn`):

1. Add `127.0.0.1  vwn.localhost` to `/etc/hosts`.
2. Run `portfoliflow create-tenant --name "Versorgungswerk Test"
   --subdomain vwn --owner-email owner@vwn.example`.
3. Log in at <http://vwn.localhost:8000/login>.

Falling back to `LOCAL_DEV_TENANT_SUBDOMAIN`: if you can't or don't
want to edit `/etc/hosts`, set
`LOCAL_DEV_TENANT_SUBDOMAIN=minathena-capital` in `.env` and hit
`http://localhost:8000`. Only one tenant is accessible at a time
this way.

---

## Code conventions

- **Python 3.11+** with modern type syntax: `list[str]` not
  `List[str]`, `str | None` not `Optional[str]`. Type hints are
  mandatory on public APIs. ADR-0006.
- **Google-style docstrings** on all public APIs. ADR-0007.
- **English** in all code, comments, docstrings, log messages,
  exception messages, and repository documentation. ADR-0008.
  Input data values may remain in source language (e.g. German
  Excel labels).
- **Conventional Commits** for every commit. `feat:`, `fix:`,
  `docs:`, `refactor:`, `test:`, `chore:`, `style:`, `perf:`,
  `ci:`, `build:`. Imperative mood. ADR-0014.
- **Module-Scope Rule.** Adding a new module touches at most three
  existing lines outside the new module. If more, that's a sign the
  registration seam is wrong. ADR-0016.
- **ADR discipline.** Every architecturally significant decision
  (layering, persistence, integration choice, security boundary,
  naming convention) gets an ADR before or alongside implementation.
  Day-to-day code choices do not need an ADR. When in doubt, err
  toward writing one. See `docs/adr/README.md`.
- **Sentinel-tenant tests.** Repository and route tests run against
  the Sentinel Tenant by default; cross-tenant isolation tests use
  a second tenant explicitly (see `tests/repositories/conftest.py`
  and `tests/web/conftest.py`).
- **Async I/O.** Repositories are async; service-layer methods that
  perform I/O are async. Pure-calculation services (under
  `services/analytics/`) are synchronous.

---

## How to implement a new module

1. **Confirm the Area.** Decide which of the nine Areas the module
   belongs to. If unclear, ask before generating code.
2. **Create the module file** under `modules/<area>/<module_name>.py`.
   Subclass `BaseModule`, decorate with `@registry.register`, define
   `module_area` (one of the nine) and `module_title`.
3. **Implement business logic** in the module. Pure-calculation
   parts go into `services/analytics/`, integration parts go into
   `services/`, repository access goes through `core/repositories/`.
4. **Add a web route** in `web/routes/` if the module is user-
   facing. The route renders a Section partial under
   `web/templates/_partials/<area>/`.
5. **Wire the Section** into the Area's long-scroll page template
   under `web/templates/_partials/areas/<area>_body.html`.
6. **Tests:** route tests in `tests/web/test_<module>_routes.py`;
   service tests in `tests/services/`; repository tests in
   `tests/repositories/` if new repository methods landed.
7. **Reference example:** see `modules/investor_communication/portfolio_review.py`
   plus `web/routes/portfolio_review.py` plus
   `web/templates/_partials/portfolio_review_section.html` for a
   recent, complete worked pattern.

---

## What not to do — ever

- Never instantiate an LLM client directly. Route through
  `get_ai_service_core()`.
- Never bypass the ToolRegistry when adding AI-callable tools. Use
  `get_tool_registry().register(...)` with an explicit Trust Class.
- Never import PyQt6 anywhere — the Qt surface is gone (ADR-0094).
- Never import `matplotlib` in `web/`.
- Never import sibling modules from `modules/`.
- Never write business logic inside FastAPI route handlers — the
  handler is glue, the logic lives in `modules/` or `services/`.
- Never reach the Postgres ORM directly. Go through the appropriate
  repository.
- Never commit work on behalf of the user. Stage with `git add`,
  propose a Conventional Commit message, wait for confirmation.
- Never create or switch branches without explicit user request.

---

## Git rules (binding)

These rules apply without exception. They must not be relaxed or
reinterpreted unilaterally.

### Branch management

- **Always work on `main`.** All work stays directly on the `main`
  branch — the branch checked out at session start. **Never create a
  new branch and never switch to another branch** on your own
  initiative. Only create or switch branches when the user explicitly
  asks you to.
- **Never create new branches.** Work exclusively on the branch
  checked out at session start.
- **Forbidden without explicit user instruction:**
  - `git checkout -b <name>`
  - `git switch -c <name>`
  - `git branch <name>`
  - `git checkout <branch>` (branch switch)
  - `git switch <branch>` (branch switch)
- If a new branch or branch switch seems substantively useful,
  **ask first** and wait for user confirmation.

### Commits

- **Never run `git commit`.** The user commits themselves. Staging
  with `git add` is permitted when it is part of the task; the
  final commit belongs to the user.
- Also: no implicit commits (e.g. via `git commit --amend`,
  `git revert`, `git cherry-pick`, `git merge` without `--no-commit`,
  `git rebase`) without explicit instruction.
- If a commit seems substantively useful, **propose it** (including
  Conventional Commit message per ADR-0014) and wait for user
  confirmation.

### Push

- **Never run `git push`.** The user pushes themselves.
- Also no implicit pushes (e.g. `git push --set-upstream`, tags,
  etc.) without explicit instruction.

### Permitted without asking

- `git status`, `git diff`, `git log`, `git show` (all read
  operations).
- `git add <path>` for files edited in the current task.

---

## Excel import data format

PortfoliFLOW imports portfolio data via a multi-sheet Excel workbook.
The format is specified in **ADR-0009**. Read the ADR before working
on import code. Per ADR-0059, the historical working name "V2"
appears in older ADRs and as the persisted DB identifier
`format_version = "v2"`; in new prose use "Excel import format" or
"Excel import file".

Key invariants the import code must respect:

- **Dynamic column discovery.** Investment columns and their labels
  are discovered from row 1 of each investment sheet. Do not
  hardcode column counts or column-letter ranges.
- **Market reference sheets** (like `interest rates`) have their own
  independent column namespace. Don't include them in the
  investment-column consistency check.
- **Empty investment columns** are valid placeholder slots — do not
  drop them.
- **Empty plan sheets** are valid (future projections not yet
  filled in).
- **Variable attribute rows** in the `Attributes` sheet — don't
  assume a fixed number.
- **Variable date ranges** in time-series sheets — don't assume a
  fixed length.
- **German labels in the data** (e.g. `Aktien`, `Typ der
  Investition`, `Währung`) are valid string content — they remain
  in source language per ADR-0008. The import code recognises them
  as data.

For detail: `services/data_normalization/investment_extractor.py`
docstring and ADR-0009.

---

## Current project status

| Layer | Status |
|---|---|
| Persistence (Postgres + RLS + Repositories) | Production |
| Web surface (FastAPI/Jinja/HTMX) | Production — primary surface |
| Nine Areas with long-scroll IA (ADR-0058, ADR-0089, ADR-0104, ADR-0107, ADR-0128) | Production |
| Planning Desk (seventh Area, ADR-0104) | Shipped (#049, 2026-07-23) — Cash Flow Planning and Scenario Analysis (#034 v1) live: baseline projection, hypothetical transactions, repace, market/FX shock, deltas-first results |
| Cases (eighth Area, ADR-0107) | Shipped 2026-07-22 — three surfaces (Open cases / Case detail / Recently closed + archive), Watch Desk arming + Journal merge, Planning Desk scenario pin, Shirley case brief + consultation pin, migration `b031` |
| Transactions (ninth Area, ADR-0128) | In progress (#061) — Area shell with placeholder sections (S3); schema `b034` and ticket service landed (S1/S2); record-flow surfaces, blotter/history and preview follow (S4–S6) |
| Investment domain, Cashflows, NAVs | Production |
| Charts, Statistics, Portfolio Analysis | Production |
| Front-Office Overview (KPI strip + chart row) | Production (ADR-0067, ADR-0072) |
| Portfolio Review (six-tile in-app) | Production; PDF export pending (roadmap A1) |
| SAA (Back Office) | Production |
| Shirley (Assistants) with Tool Registry | Production |
| Report Scraper (Assistants) | Production |
| Providers & Credentials (Admin) | Production (ADR-0112 §6) — scoped-settings write surface; replaced the ADR-0052 runtime AI-Settings form |
| Anlagegrenzen / Limits / AUM (Phase 7) | Production read-only surface at /back-office#limits (ADRs 0055/0056/0057, migration b010); the coverage denominator is Σ NAV since ADR-0103 §2 (migration b030 dropped `portfolio_aum`); editing surface deferred (roadmap #019 follow-ups; the former B5d Portfolio-Review tile is won't-do) |
| Watch Desk watchpoints and signal families (ADR-0116) | In progress (#057) — migration `b033`: historised `watchpoints` + `floor_calibration`, the Calibration editor and watchpoint list, per-subject sensitivity overlays, and the four signal families (`price`, `fx`, `freshness`, `liquidity`) live on the monitor and in the beat |
| Benchmarks & Attribution (Back Office) | Production (ADR-0061, migration b011) |
| Liquid asset archetypes (fixed income / listed equity) | Production (ADRs 0079–0083, migrations b016–b018) |
| Qt GUI | Removed July 2026 (ADR-0094 Stage 1); web is the sole surface |
| Telegram bot (`bot/`) | Optional sibling channel (ADR-0030) |

For planned work the canonical source is `docs/roadmap.md`. For
historical phase artefacts see `docs/_archive/phase-5-followups.md`.

---

## Implemented services — quick reference

ADRs are the authoritative source. This table groups ADRs by topic;
read the linked ADR before touching the corresponding area.

| Topic | ADRs |
|---|---|
| Layering, modules, naming | ADR-0001, ADR-0002, ADR-0003, ADR-0016, ADR-0044, ADR-0084 (glossary v2, supersedes ADR-0002) |
| Product scope | ADR-0074 |
| Process and conventions | ADR-0006, ADR-0007, ADR-0008, ADR-0014, ADR-0015, ADR-0059 |
| Lint, typecheck and CI | ADR-0109, ADR-0110 (typing island set; supersedes ADR-0109 §3 in part) |
| Persistence (Postgres, RLS, Strangler) | ADR-0034, ADR-0035, ADR-0041, ADR-0077, ADR-0078 |
| Web migration and architecture | ADR-0033, ADR-0037, ADR-0039, ADR-0058 |
| Authentication and bootstrap | ADR-0036, ADR-0040 |
| Excel import and investment domain | ADR-0009, ADR-0043 |
| Charts and analytics | ADR-0013, ADR-0042, ADR-0045 |
| Theming | ADR-0021, ADR-0025, ADR-0032 |
| Reporting and Portfolio Review | ADR-0020, ADR-0026 |
| Region model | ADR-0046 |
| AI service and tools | ADR-0010, ADR-0011, ADR-0012, ADR-0028, ADR-0038, ADR-0047 |
| Tool trust and external content | ADR-0022 |
| Web research and scrapers | ADR-0023, ADR-0024, ADR-0027 |
| Shirley (Assistants area) | ADR-0048, ADR-0049, ADR-0050, ADR-0051, ADR-0052 |
| Report Scraper (web surface) | ADR-0053 |
| SAA consolidation | ADR-0054 |
| Anlagegrenzen (Phase 7) | ADR-0055, ADR-0056, ADR-0057, ADR-0083 |
| Liquid archetypes | ADR-0079, ADR-0080, ADR-0081, ADR-0082 |
| Telegram bot | ADR-0030, ADR-0031 |
| Multi-user readiness | ADR-0019 |
| Irene / Watch Desk | ADR-0085, ADR-0086, ADR-0087, ADR-0088, ADR-0089, ADR-0106, ADR-0115 (rename), ADR-0116 (watchpoint registry and signal families) |
| Live market data and identifiers | ADR-0090, ADR-0091, ADR-0092, ADR-0093, ADR-0095, ADR-0096 |
| Position model | ADR-0097, ADR-0098 |
| Multi-currency and cash | ADR-0099, ADR-0100, ADR-0101, ADR-0102, ADR-0103 |
| Planning Desk, overlays, TA pacing | ADR-0104, ADR-0105 |
| Cases | ADR-0107 |
| Licensing and contribution apparatus | ADR-0108 |
| Section topology (placeholder retirement) | ADR-0111 |
| Scoped settings and credentials | ADR-0112 |
| Front-office chart conventions | ADR-0113 |
| Chart snapshot persistence | ADR-0114 |

### Hard rules carried forward

These rules apply even before reading the relevant ADR:

- Never instantiate an LLM client directly. Use
  `get_ai_service_core()`.
- Never bypass the ToolRegistry when adding AI-callable tools.
  Register with `get_tool_registry()` and declare a Trust Class.
- Content from `READ_EXTERNAL_UNTRUSTED` tools must pass through the
  Fetcher-LLM (`send_one_shot_extraction`) and be wrapped in
  `<external_content trust="untrusted">…</external_content>` before
  reaching Shirley's conversation.
- Once a `READ_EXTERNAL_UNTRUSTED` tool has fired in a turn,
  `WRITE_INTERNAL` and `EXTERNAL_EFFECT` tools are locked for the
  rest of that turn.

---

## Asking for clarification

If a prompt is ambiguous about which Module to modify, which Area a
new Module belongs to, or whether a dependency is acceptable, ask
before generating code. A wrong assumption that touches existing
files costs more to undo than a clarifying question costs to answer.

---

## Planned work

The canonical source for planned work is `docs/roadmap.md`. Do not
implement roadmap items unsolicited; if asked for one, read the
linked ADR first and confirm the scope before writing code.

Legacy A/B/C/D roadmap IDs still cited in ADRs and docs (e.g. B2, A1,
B5d) resolve to the flat `#NNN` roadmap IDs via the crosswalk in
`docs/roadmap.md`; the legacy IDs are intentionally retained and not
renumbered.

For historical phase artefacts (the Phase-5 follow-ups), see
`docs/_archive/phase-5-followups.md` — that document is frozen as a
historical record and is not a source of new work.

---

## Legacy: the PyQt6 GUI (removed)

The `gui/` PyQt6 desktop surface was removed in July 2026 (ADR-0094
Stage 1); the web variant is now the sole surface, and PyQt6 must not
be reintroduced anywhere. The pre-removal state is preserved at the
`demo-stable-pre-qt-sunset` git tag; git history and the
`demo-stable-*` tags remain the archival record.

The in-memory `core.data_store.DataStore` the GUI relied on is not
removed with it: it survives under the Strangler coexistence
(ADR-0041), now consumed only by the DataStore-coupled reporting
engine and a few module shells, and is staged for removal in
Qt-sunset Stage 2 (ADR-0094 §5, roadmap #035). New code must not
depend on it.
