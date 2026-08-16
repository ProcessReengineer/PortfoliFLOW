# ADR-0033: Web Migration — Architectural Shift from PyQt6 Desktop to FastAPI Web

- **Status:** Accepted
- **Date:** 2026-05-03
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, architecture, process, integration

---

## Context

PortfoliFLOW is, today, a PyQt6 desktop application. The architecture
established in ADR-0001 separates `core/`, `services/`, `analytics/`,
`modules/`, and `gui/` into named layers with one-way imports, and the
business-logic layers are deliberately Qt-free wherever possible. Data
lives in the in-memory `DataStore` singleton (ADR-0004), with Excel as
the import source. The application is shaped around a single operator
sitting in front of one machine, and every shipped feature so far —
SAA, charts, statistics, Shirley, Report Scraper, Phase-1 reporting —
runs in that single-user desktop frame.

The forces that motivate revisiting that frame have accumulated over
the past six months:

- **Multi-user demand.** FoF boutiques and Versorgungswerke are not
  single-operator organisations. Several team members need controlled
  access to the same investments, SAA models, and reports. ADR-0019
  has anticipated this and required the audit fields that make
  multi-user retrofits cheaper, but no multi-user code has been
  written, and the current process model (one in-memory DataStore per
  process) cannot host it.
- **Reach.** A browser is the universal client. Maintaining a desktop
  app on Linux, Windows, and macOS — plus eventually a mobile surface
  — is disproportionate for a solo developer. The browser is the
  cheapest path to a coherent UX across operating systems and form
  factors.
- **Deployment economics.** Centralised updates, a defined runtime
  environment, and a homogeneous browser target reduce operations
  cost relative to shipping desktop binaries. Every fix lands once.
- **Compliance posture.** Versorgungswerke and institutional LPs
  expect centralised auditability, controlled access, and documented
  authentication. These properties emerge naturally from a web
  architecture; in a desktop deployment they have to be reconstructed
  on top of a runtime that was not built for them.
- **Architectural foreshadowing.** The layered codebase has been
  preparing for this. ADR-0011 named the PyQt6 dependency in
  `AIService` as a future split target the moment a non-Qt consumer
  appeared. ADR-0029 introduced `services/headless_shirley.run_turn`
  as a Qt-free entry point. ADR-0030 wired the Telegram bot as the
  first non-GUI client of that entry point. ADR-0017 planned DuckDB
  as the persistent data layer for a single-user desktop deployment —
  a decision whose premise no longer holds once multi-tenant
  concurrency is required. ADR-0018 named the Service / Repository
  layering that the client-server topology depends on.

This ADR is the strategic frame for the migration. It commits to the
direction, the migration pattern, and the phased plan. It does not
prescribe the technical details of persistence, multi-tenancy,
authentication, frontend stack, or AIService refactoring; each of
those is the subject of a dedicated detail ADR (ADR-0034 through
ADR-0038), and the migration pattern itself is recorded in ADR-0039.

This decision is architecturally far-reaching and audit-relevant.
BAIT and VAIT expect documented architectural decisions for the
platform that supports regulated workflows; ISO 25010 quality
attributes (Maintainability, Security, Portability, Reliability) are
all touched; DORA's operational-resilience requirements become
applicable in earnest once the application becomes multi-tenant and
network-reachable.

## Decision

PortfoliFLOW will migrate from a PyQt6 desktop application to a
web application. The desktop variant remains functional throughout
the migration and will be marked deprecated only after the web
variant has demonstrably reached functional parity (separate ADR at
that point). The following commitments together constitute this
decision:

1. **Web stack: FastAPI + Jinja templates + HTMX.** Plotly.js for
   charts, Tabulator.js or AG-Grid for complex tables. No Single
   Page Application framework. Detail in ADR-0037.

2. **User model: multi-tenant with sharing inside a tenant.**
   Authorisation boundaries are tenant-scoped; users inside a
   tenant can share investments, SAA models, and reports under
   role-based access. Detail in ADR-0035.

3. **Persistence: PostgreSQL.** This supersedes the DuckDB choice
   recorded in ADR-0017. The DuckDB premise (single-writer,
   embedded, file-based) does not survive multi-tenant concurrency
   and row-level security. Detail in ADR-0034.

4. **Authentication: Phase-2 local session-based, structurally
   prepared for OIDC.** Sessions are issued by the application;
   the schema and middleware are shaped so that an OIDC backend
   (Keycloak, Authentik, or a customer's IdP) can be added without
   touching the application code. Detail in ADR-0036.

5. **AIService: split into a Qt-free core and a Qt adapter.** This
   discharges the follow-up that ADR-0011 named when it accepted
   the PyQt6 import in `services/ai_service.py`. The split also
   completes the strangler pattern begun by `headless_shirley`
   (ADR-0029). Detail in ADR-0038.

6. **Migration pattern: Strangler.** Both worlds (PyQt6 and Web)
   run in parallel for the duration of the migration and share
   `core/`, `services/`, `analytics/`, `modules/` as a single,
   common backend. Detail in ADR-0039.

7. **Phased plan.** The migration is sequenced across six phases.
   Each phase concludes with a demo-stable state on `main`:

   - **Phase 0 — Pre-migration ADRs.** This ADR plus six detail
     ADRs (0034–0039). No code changes.
   - **Phase 1 — Foundations.** Decouple `AIService` (Qt-free
     core + Qt façade). Make the in-memory `DataStore` persistable
     against Postgres. Define the Postgres schema and the
     Repository layer. PyQt6 GUI continues to operate against
     the same backend code throughout.
   - **Phase 2 — First end-to-end web slice.** A FastAPI backend
     with sentinel auth (single-tenant, single-user) serves one
     end-to-end endpoint: Shirley chat through the browser.
     Multi-tenant data model is structurally active (one sentinel
     tenant) but not yet user-visible.
   - **Phase 3 — Design system extracted, SAA as comparison test.**
     `config/ui_theme.json` and `config/chart_theme.json` (and
     the planned schema extension in ADR-0032) become the canonical
     source for both Qt stylesheets and web CSS. The SAA area is
     re-built in the web — a self-contained area used as visible
     proof that the web variant is functionally equivalent to the
     desktop one.
   - **Phase 4 — Remaining areas ported.** Investments, Charts &
     Statistics, Excel Import, Documents, and the remaining
     modules.
   - **Phase 5 — Multi-user activated.** Auth is hardened (OIDC
     backend optional, MFA discussion). Tenant isolation is
     finalised at production grade. Backup and DR strategy is
     finalised against the chosen deployment model.

8. **Out of scope / deliberate non-decisions.** The following
   choices are explicitly *not* made by this ADR and are recorded
   so future contributors do not have to guess:

   - **No SPA (React, Vue, Svelte) as the primary frontend.**
     Punctual React or Alpine.js components are admissible at
     individual call sites where HTMX is insufficient, but no
     project-wide SPA build stack is adopted.
   - **No native mobile application.** Browser access is sufficient.
   - **No frontend build stack beyond what HTMX and a small set of
     CDN-delivered JavaScript libraries require.**
   - **No commitment to a deployment topology (on-premise vs.
     EU-cloud vs. multi-tenant SaaS).** The architecture supports
     all three. Initial deployment is on-premise as the primary
     model with structural preparation for EU-cloud hosting; SaaS
     remains an option that can be activated later without a
     rewrite.

## Rationale

- **FastAPI keeps the backend in Python.** The shared layers
  (`core/`, `services/`, `analytics/`, `modules/`) are Python
  today and will be reused by the web. A Python backend is the
  cheapest way to keep the strangler pattern intact: any other
  choice would require a foreign-language tier between the web
  layer and the existing code. FastAPI's async model, native
  Pydantic integration, and OpenAPI generation are aligned with
  the AIService and ToolRegistry patterns the project already
  uses.
- **HTMX over an SPA reflects the developer-resource constraint.**
  Building a parallel SPA codebase (build tooling, state
  management, routing, schema validation between client and
  server) is not realistic for a solo developer maintaining the
  desktop variant in parallel. HTMX captures the substantial
  majority of the UX value at a small fraction of the build-stack
  complexity, and the FastAPI side speaks plain HTML fragments —
  a debugging seam that survives every browser. Where HTMX is
  genuinely insufficient for a specific widget, a punctual React
  or Alpine.js component is admissible (this preserves the
  optionality without forcing a project-wide commitment).
- **Multi-tenant from the start avoids a schema migration later.**
  Single-tenant with the intention to migrate is the more
  expensive path: every table either gets a `tenant_id` retrofit
  (which is the migration we chose to avoid) or stays
  single-tenant forever. Adding `tenant_id`, RLS policies, and
  audit triggers from day one in Phase 1 costs one to two days
  more than the single-tenant variant; doing it later costs a
  schema migration plus a coordinated deployment. The
  decision pays for itself the first time a second tenant
  appears.
- **Postgres because DuckDB does not fit the new premise.**
  ADR-0017's DuckDB choice was correct under the desktop
  single-user premise. Under multi-tenant concurrency with
  row-level security and audit triggers, DuckDB is the wrong
  tool; the decision is recorded in ADR-0034 and supersedes
  ADR-0017 there.
- **Strangler pattern protects demo capability.** The
  `headless_shirley` / Telegram-bot pair (ADR-0029, ADR-0030)
  is the lived precedent: parallel surfaces around a shared
  backend, with a Qt-free invariant enforced by a regression
  guard. Generalising that pattern to the web migration keeps
  the codebase demo-stable to prospective LPs and GP targets
  through every intermediate state. A big-bang migration would
  trade demo capability for migration speed, and demo capability
  is the more valuable asset during the soft-pitch phase.
- **Phasing in this order optimises for the highest-risk
  unknowns first.** AIService refactoring is the bridge between
  the Qt and web worlds; doing it in Phase 1 unblocks Phase 2's
  end-to-end slice. The end-to-end slice in Phase 2 validates the
  full web stack against a real workload (Shirley) before any
  area-by-area porting begins. The SAA area in Phase 3 is the
  comparison test: a self-contained, well-understood area whose
  web reimplementation can be benchmarked against the desktop
  one for visual and functional parity. Bulk porting in Phase 4
  follows the path the Phase 3 work has cleared.
- **Optionality on the deployment model is preserved
  intentionally.** The choice of on-premise vs. EU cloud vs.
  SaaS depends on customer conversations that have not happened
  yet; an architecture that can host all three keeps that
  conversation open without forcing a re-decision.

## Alternatives Considered

- **Status quo: keep PyQt6 as a desktop application.**
  Rejected. The strategic vision (a lean, AI-augmented
  fund-management platform usable by institutional teams)
  requires multi-user access on a universal client. A desktop
  app rules out the institutional segment and forecloses the
  reach that motivates the project.
- **SPA architecture (React or Vue).**
  Rejected. The cost of maintaining a separate frontend codebase
  — build tooling, state management, routing, schema/version
  coordination between server and client — is not absorbable by
  a solo developer who is also maintaining the desktop variant
  during the migration. HTMX yields a large fraction of the SPA
  UX at a small fraction of the cost. Punctual React or Alpine.js
  components remain available where HTMX is not sufficient,
  preserving the option to upgrade specific widgets without
  paying the SPA tax everywhere.
- **Native applications (PyQt6 plus a native mobile app).**
  Rejected. Three codebases is two more than the project can
  sustain. The browser already handles the mobile surface
  acceptably for the use cases foreseen.
- **Continue with DuckDB as planned in ADR-0017.**
  Rejected. DuckDB's strengths (embedded, columnar, single-file)
  are oriented to single-writer analytical workloads. Multi-tenant
  concurrency, row-level security, and audit-trigger enforcement
  are outside its operating envelope. Detail in ADR-0034, which
  supersedes ADR-0017.
- **Big-bang migration (rebuild on the web stack and switch over
  in one step).**
  Rejected. The demo-stable property is non-negotiable during
  the soft-pitch phase: a multi-month "system is being rebuilt"
  state would interrupt the ability to show a working product to
  prospective LPs and GP targets. The strangler pattern is the
  proven local alternative (ADR-0029, ADR-0030) and protects
  demo capability throughout. Detail in ADR-0039.
- **Defer the migration until the desktop product reaches
  feature completeness.**
  Rejected. Several near-term feature requests (Report Scraper
  workflow with multiple operators, shared SAA models, audit-grade
  access control) hit the desktop architecture's ceiling. Adding
  multi-user retrofits to a desktop app is more expensive than
  building the web app once.

## Consequences

### Positive

- Multi-user access becomes structurally feasible without
  rewriting the existing backend code.
- Browser reach is achieved without native application
  development.
- The strangler pattern preserves demo capability throughout
  the migration — the project remains pitch-ready at every
  intermediate state.
- The phased plan permits incremental validation: each phase
  ends in a demo-stable, tagged state on `main`, so a missed
  schedule does not invalidate prior work.
- The architecture is shaped to host on-premise, EU-cloud, and
  SaaS deployment models. Strategic optionality on the business
  side is preserved without a re-architecture.
- The follow-up named in ADR-0011 is finally discharged in
  Phase 1 (via ADR-0038), rather than carried indefinitely as
  a layering exception.

### Negative

- Parallel maintenance during the migration. The PyQt6 GUI and
  the web layer both receive bug fixes that touch shared
  backend code. Doubled testing effort applies until the
  desktop variant is deprecated.
- Stack complexity grows: FastAPI, Jinja, HTMX, Postgres,
  Plotly.js, Alembic, and a small set of CDN-delivered JS
  libraries are added to PyQt6's existing footprint. CVE
  surveillance, dependency updates, and configuration grow
  proportionally.
- Multi-tenant data modelling has a higher initial complexity
  ceiling than single-user (RLS policies, `tenant_id` on every
  table, audit triggers). The cost is one to two days in Phase 1;
  the avoided cost is a schema migration in Phase 5.
- Solo-developer concentration risk. The migration is
  substantial; if competing priorities pause it, the project
  could spend an extended period in a half-migrated state. This
  is mitigated, not eliminated, by the demo-stable phase
  discipline of ADR-0039.
- Network-reachable surface area shifts the security model.
  Today's "single trusted user, no remote access" assumption
  (acknowledged in ADR-0022's gating policy) becomes invalid
  the moment the FastAPI surface is on a network. ADR-0036
  records the authentication countermeasures.

### Neutral / Follow-ups

- Backend language stays Python across all layers. No new
  language tier in the backend; only the frontend gains a
  small JavaScript footprint.
- Test strategy gains an HTTP integration tier (FastAPI
  TestClient, possibly Playwright for end-to-end browser
  scenarios). Domain-layer unit tests remain unchanged in
  shape and value.
- A Repository layer (ADR-0018) becomes a Phase-1
  precondition rather than an aspirational follow-up.
- The eventual deprecation of the PyQt6 layer is not decided
  here. A separate ADR will record that decision when the web
  variant has demonstrably reached parity and at least three
  months of stable demo use.

## Implementation Notes

- **Branch strategy.** The migration runs on `web-migration`;
  `main` remains the demo-stable branch. Tagging convention
  `demo-YYYY-MM-DD`. Detail in ADR-0039.
- **Directory structure.** Existing layers (`core/`,
  `services/`, `analytics/`, `modules/`) remain unchanged. New
  top-level directories for the web tier: `web/` (FastAPI
  application, routes, templates, static), `web/templates/`
  (Jinja), `web/static/` (CSS, JavaScript, assets). Database
  schema and migrations under `db/migrations/` (Alembic).
- **Configuration.** `config/ui_theme.json` and
  `config/chart_theme.json` remain the canonical sources of
  design tokens. PyQt6 stylesheets and web CSS are generated
  in parallel from the same tokens; the schema extension
  recorded in ADR-0032 is consumed by both targets. Detail
  in ADR-0037.
- **Persistence wiring.** The in-memory `DataStore` is wrapped
  by a Postgres-backed implementation in Phase 1; both the
  PyQt6 GUI and the web tier consume the same store through
  the Repository layer. Detail in ADR-0034.
- **AIService split.** The Qt-free core moves into a module
  consumable by both the FastAPI handler and the existing
  PyQt6 widgets via a thin adapter. Detail in ADR-0038.
- **No code changes in this ADR.** Phase 0 produces only
  documentation; implementation begins in Phase 1.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (eliminating doubled GUI maintenance after the migration),
  Security (centralised authentication and tenant isolation
  replace the implicit "single trusted user" assumption),
  Portability (browser targets replace per-OS desktop
  binaries), Reliability (deployment-model-specific backup
  and DR are addressed in Phase 5).
- **Regulatory references:** BAIT AT 7.2 and the analogous
  VAIT controls expect documented architectural decisions for
  the platform that supports regulated workflows; this ADR is
  the head of that documentation chain. DORA's operational-
  resilience expectations apply once the application becomes
  multi-tenant and network-reachable; the deployment-specific
  measures land in Phase 5 and are out of scope here.
- **Audit evidence:** This ADR; the detail ADRs (0034 through
  0039); the phase-end demo tags on `main` (per ADR-0039); the
  CLAUDE.md update that records the new directory layout and
  layering rules at the time of Phase 1.

## References

- ADR-0001 (Layered Architecture and Strict One-Way
  Dependencies) — remains in force; the web tier is added as
  an additional layer with the same one-way-import discipline.
- ADR-0004 (In-Memory DataStore Singleton) — the working-copy
  semantics remain valid; persistence is added underneath via
  the Repository layer.
- ADR-0011 (Acknowledged PyQt6 Dependency in AIService) — the
  follow-up it named is discharged in ADR-0038.
- ADR-0017 (Planned DataVault — DuckDB) — superseded by
  ADR-0034.
- ADR-0018 (Planned Service / Repository Layering) — becomes
  a Phase-1 precondition rather than a future plan.
- ADR-0019 (Planned Multi-User Readiness) — concretised by
  ADR-0035 (multi-tenancy data model) and ADR-0036
  (authentication).
- ADR-0022 (Tool Trust Classes and Gating Policy) — its
  "single trusted user" assumption is structurally lifted by
  ADR-0036.
- ADR-0029 (Headless Shirley as Qt-Free Synchronous Entry
  Point) — the strangler precedent on the AI side.
- ADR-0030 (Telegram Bot as First Non-GUI Client) — the
  strangler precedent on the channel side.
- ADR-0032 (UI Theme Schema Extension) — the design-token
  source consumed by both Qt stylesheets and web CSS.
- ADR-0034 (Persistence: PostgreSQL Replacing DuckDB) — detail.
- ADR-0035 (Multi-Tenant Data Model with Intra-Tenant Sharing) —
  detail.
- ADR-0036 (Authentication: Phase-2 Local Sessions, OIDC-Ready) —
  detail.
- ADR-0037 (Web Frontend Stack: FastAPI + Jinja + HTMX) —
  detail.
- ADR-0038 (AIService Refactoring: Qt-Free Core with Qt
  Adapter) — detail.
- ADR-0039 (Migration Pattern: Strangler with Tagged
  Demo-Stable Branch) — pattern.

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-05-03 | PortfoliFLOW project owner   | Initial draft. Records the strategic frame for migrating PortfoliFLOW from a PyQt6 desktop application to a FastAPI-based web application, the phased plan, and the deliberate non-decisions. Detail decisions are split across ADR-0034 to ADR-0039. |
| 2026-05-06 | PortfoliFLOW project owner   | Phase 3 (SAA migration) complete on `web-migration`. Read + write surfaces, audit / RLS / CSRF / tenant-isolation green, numerical MVO identity verified by construction. No merge to `main` (per ADR-0042 governance). Acceptance report at `docs/phase-3-acceptance-report.md`; visual sign-off pending. Detail entries in ADR-0042. |
| 2026-05-20 | PortfoliFLOW project owner   | Promoted to Accepted. The PyQt6-to-FastAPI/Jinja2/HTMX migration is complete as of Phase 6 Block 1; the web surface is the primary user surface. Closes P6-E. |
