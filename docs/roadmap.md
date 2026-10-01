# PortfoliFLOW — Roadmap

**Status:** feature-complete since 2026-09-29 — refinement of the existing Areas;
no new Areas and no wholly new functional areas.
**Last updated:** 2026-09-30
**Owner:** Soenke (ProcessReengineer)

This is the steering document for PortfoliFLOW: what is being worked on, what
comes next and in which order, which ideas are on file, what was decided
against, and what has shipped. It records no history — that lives in git and in
[`CHANGELOG.md`](../CHANGELOG.md). Design and rationale live in the ADRs
([`docs/adr/`](adr/README.md)); on any conflict, **the ADR wins**. Terms follow
[`docs/glossary.md`](glossary.md).

**How to read it.**

- Items carry a flat running number (`#001`, `#002`, …) that never encodes the
  bucket and is never reused. **Next free ID: `#071`.**
- An ID sits in exactly one bucket. An item that shipped in part stays where its
  remaining scope is, and its block says what shipped; the Shipped record lists
  items with nothing left. Where a finished item left a separate remainder,
  that remainder is an unnumbered idea naming its origin.
- **Active** is in progress now. **Next** is ranked: the order is the
  priority. **Ideas** are unprioritised future directions; an idea becomes an
  item in Next only by an owner decision, and a new Area or a wholly new
  functional area is not taken up while the feature-complete principle holds.

---

## Active

### #067 — Provider channel Stage B: directory, relay, portal

- **Purpose:** the operated half of the provider channel — the signed directory
  published on portfoliflow.com, a zero-knowledge relay, a provider portal, and
  the instance-side arming of the Stage A contract.
- **Shipped so far (stage B-1):** the publishing-key ring, directory
  verification by key id, the `provider_channel.enabled` setting, the
  `x25519-sealed-box` export contract, and the directory fetch/cache client
  with the `directory-refresh` / `directory-status` commands.
- **Remaining:** the B-1 instance side (the `enabled` switch's consumer, the
  refresh timer and gesture, the directory panel, the suggestion filter and
  the export gesture), then B-2 relay and B-3 portal; the `engagements` object
  last and droppable. Test providers only; real providers are the named
  successor "Stage B.1"; no remuneration mechanics (Stage C is legal-gated).
- **ADR:** ADR-0129; decisions of record in
  [`docs/concepts/provider-channel-stage-b-decisions.md`](concepts/provider-channel-stage-b-decisions.md).
- **Depends on:** #061 (shipped).

### #069 — Watch Desk refinement: briefing artefact, watch-point scope, presets

- **Purpose:** make the Watch Desk's proactive role visible to an average
  portfolio manager — the first refinement item after feature-completeness.
- **Scope:** Irene writes a two-part briefing in a second synthesis call after
  the deterministic floor (markets and press read against the whole priceable
  book, then the watched families), persisted per check; an owner-only on/off
  switch per signal family, separate from mute; sensitivity presets Quiet ·
  Standard · Nervous; every check as a Journal row; "Discuss briefing with
  Shirley"; Irene is never presented to herself as Shirley.
- **ADR:** ADR-0133 (annex amendments to ADR-0088 and ADR-0116).
- **Depends on:** #033, #057 (both shipped). UX strand A-6 of #070 restyles the
  surface afterwards.

### #070 — UX overhaul and user manual

- **Purpose:** bring every Area to one consistent, calmer interface — one
  Section per view, intent first, Shirley dockable in every view — and write the
  user manual on top of it.
- **Shipped so far:** the shell, design tokens and component layer (A-0) and the
  Transactions Area as the pilot (A-1).
- **Remaining, in order:** A-2 Back Office, A-3 Cases, A-4 Admin and My
  settings, A-5 Front Office and the shell, A-6 Watch Desk (UI only, after
  #069), A-7 Assistants; then the user manual.
- **ADR:** ADR-0134 (Section as one view); conventions in
  [`docs/ux/ui-standards.md`](ux/ui-standards.md).
- **Depends on:** none; A-6 on #069.

---

## Next

| Rank | ID | Title | ADR |
|---|---|---|---|
| 1 | #065 | openai SDK 3.x migration | — |
| 2 | #066 | Installer: `--doctor` re-resolves the engine | ADR-0124 §1.1 |
| 3 | #064 | Provider retry policy in the tick | ADR-0091, ADR-0125 |
| 4 | #035 | DataStore decommission | ADR-0094 §5 |
| 5 | #041 | Functional-currency identifier renames (`*_eur`) | ADR-0099, ADR-0101 |
| 6 | #019 | Limit-set editing | ADR-0055–0057 |
| 7 | #046 | Portfolio Review restructure (hierarchical tile sets) | — |
| 8 | #001 | Portfolio Review PDF export and detail areas | ADR-0073 §5, ADR-0020 |
| 9 | #063 | Market-data trading-hours awareness | ADR-0125 |
| 10 | #042 | Live FX-rate supply | ADR-0099 §5, ADR-0091 |
| 11 | #025 | Hosted deployment (Hetzner) | — |
| 12 | #036 | Bloomberg live smoke | ADR-0090 – ADR-0093, ADR-0096 |
| 13 | #007 | Investment sub-class field | — |
| 14 | #031 | Sample-data fidelity follow-ups | ADR-0081 |

### #065 — openai SDK 3.x migration

- **Purpose:** lift the `openai<3` bound in `pyproject.toml`, which is a freeze,
  not a fix: 3.x moved its HTTP layer to `httpx2`, which the test doubles do not
  intercept.
- **Remaining:** move the LLM and voice clients to 3.x and the test doubles off
  transport-level `httpx` mocking; then remove the bound.

### #066 — Installer: `--doctor` re-resolves the engine

- **Purpose:** `scripts/install.sh --doctor` resolves the container engine by
  precedence again instead of using the one chosen at install time, so on a host
  with both engines it can inspect the wrong one.
- **Remaining:** persist the resolved engine in `.env` at install time; doctor
  reads it before falling back to precedence.
- **ADR:** ADR-0124 §1.1.

### #064 — Provider retry policy in the tick

- **Purpose:** a transient provider failure (timeout, rate limit, 5xx) costs an
  investment its whole refresh round; ADR-0091 assigned retries to the tick and
  none were built.
- **Remaining:** bounded retries with backoff for `ProviderFetchError`, never
  for `IdentifierNotResolvableError`; per investment, inside the existing
  failure isolation.
- **ADR:** ADR-0091 (assignment), ADR-0125 §Consequences.

### #035 — DataStore decommission

- **Purpose:** retire the in-memory DataStore complex left behind by the Qt
  sunset (`core/data_store.py`, `core/persistent_data_store.py`, the
  DataStore-bound report engine and its module shells).
- **Remaining:** a short ADR scoping the removal; relocate `load_excel` off the
  DataStore path; keep `compute_irr`; drop `data_store_entries` by forward
  migration; re-check the `matplotlib` / `squarify` dependencies.
- **ADR:** ADR-0094 §5. **Depends on:** #001 confirming the bundle-based render
  path needs no DataStore.

### #041 — Functional-currency identifier renames (`*_eur`)

- **Purpose:** values and labels are in the tenant's functional currency, but
  DTO fields and template keys still say `_eur`.
- **Remaining:** one pure rename `*_eur` → `*_functional` across DTOs and the
  context keys built from them, with no behavioural change in the same diff.
  (The `portfolio_aum.aum_eur` column is gone since migration `b030`.)
- **ADR:** ADR-0099 §Follow-ups, ADR-0101 §3; ADR-0044 as rename precedent.

### #019 — Limit-set editing

- **Purpose:** investment-limit monitoring is live read-only at
  `/back-office#limits` (engine, Excel import and surface shipped 2026-05-20);
  limit sets can still only change through the workbook.
- **Remaining:** an edit mode for limit sets — validation, historisation by
  `effective_from` (limit sets stay immutable), audit trail.
- **ADR:** ADR-0055, ADR-0056, ADR-0057.

### #046 — Portfolio Review restructure (hierarchical tile sets)

- **Purpose:** show the whole book hierarchically, with no filter interaction:
  the portfolio aggregate, then one aggregate per sub-asset class, then one
  six-tile set per investment.
- **Remaining:** everything; aggregation reuses the converted review seam
  server-side (`investment_ids`). A short surface ADR at kickoff.
- **Depends on:** #013 (shipped). Starting basis for #047.

### #001 — Portfolio Review PDF export and detail areas

- **Purpose:** turn the review into a report an investor can pull at any time,
  exportable as PDF, with detail areas and reproducible snapshots.
- **Remaining:** PDF export over the existing bundle contract (server-side
  Plotly via `kaleido`, ADR-0073 §5); detail areas; branding. PDF vs. HTML with
  print CSS is still open.
- **ADR:** ADR-0073 §5; ADR-0020 (Proposed — the three-layer reporting design).
- **Depends on:** #046.

### #063 — Market-data trading-hours awareness

- **Purpose:** at sub-hourly cadence a tenant spends provider calls all night
  and weekend re-reading prices that cannot change.
- **Remaining:** skip intraday price fetches outside the instrument's exchange
  session; needs an exchange-calendar source and a per-identifier session
  lookup. Concept decision at kickoff (calendar source, where the skip is
  enforced, effect on `last_run_at`).
- **ADR:** commissioned by ADR-0125 §Consequences. **Depends on:** #036.

### #042 — Live FX-rate supply

- **Purpose:** FX rates enter only through the workbook; a missing pair blocks
  conversions and the Planning Desk by design (no silent 1:1 fallback).
- **Remaining:** an `fx_rate` series kind, capability entry and adapter —
  ECB SDMX preferred; decide how a rate series is keyed, since a currency is not
  a security. `FxRateRepository.upsert_live` is already waiting.
- **ADR:** ADR-0099 §5, ADR-0091, ADR-0092. **Depends on:** #036.

### #025 — Hosted deployment (Hetzner)

- **Purpose:** a production instance for the first users other than the owner.
  The local installer (ADR-0124) does not cover it.
- **Remaining:** server hardening, reverse proxy and TLS, container vs. systemd,
  Postgres backup and restore test, secrets, logging, monitoring, CD; tenant
  routing by subdomain vs. path.
- **Depends on:** #015 (shipped).

### #036 — Bloomberg live smoke

- **Purpose:** Live Data Import is built (identifiers, provider port and
  adapters, live-ingest contract with Excel precedence, trigger and tick,
  provider-native fund identifiers). The Bloomberg Desktop-API adapter is
  validated against fixtures only and ships `enabled: false`.
- **Remaining:** one live run against an entitled Terminal — install `blpapi`,
  enable the adapter, run a tenant-scoped manual tick. Operator-gated.
- **ADR:** ADR-0090 – ADR-0093, ADR-0096.

### #007 — Investment sub-class field

- **Purpose:** the workbook's "Investment Sub-Class" row (e.g. "Small Cap
  Europe") is discarded on import; the database has no field for it.
- **Remaining:** a nullable column by migration, the extractor reading it, and
  the investment headers showing `{Name}, {Asset class}, {Sub-class}`.

### #031 — Sample-data fidelity follow-ups

- **Purpose:** accepted imprecisions of the liquid-archetype sample data.
- **Remaining:** regenerate the showcase bonds with ex-distribution drops and
  re-verify the two intentional SAA breaches; re-examine the `credit` alias
  against real manager labels; historise equity sector and region composition;
  route listed funds to the total-return surface instead of private-markets
  multiples. (Cash treatment is settled by ADR-0100 and ADR-0103.)
- **ADR:** ADR-0081.

---

## Ideas

Unprioritised. One paragraph each; none is scheduled.

**#003 — Due-diligence support.** A sibling of the Report Scraper on the same
extraction backend for GP due-diligence documents (PPMs, LPA drafts): fund size,
strategy, track record, key-person clauses.

**#017 — Shirley: code generation and execution.** Shirley writes a Python or
charting snippet that runs in a sandbox with a library allowlist, under a
per-user trust setting and an audit log.

**#018 — Shirley: full app control.** Shirley operates every function of every
Area through tools, never beyond what the user may do. Carries the remainder of
#015: role-bound tool trust (ADR-0022 §4) only has a subject once Shirley has
write tools — today none is registered.

**#020 — Shirley: analysis reads, phase 2.** Deterministic read tools for the
remaining surfaces a user sees — portfolio review, benchmark comparison,
efficient frontier (portfolio overview and SAA configuration exist). Carries the
remainder of #022: a tenant-scoped dataset context and explicit negative hints
in the system prompt. ADR-0070 (Proposed, stub).

**#021 — Persistent analysis results.** A tenant-scoped store for run-bound
results — first the Report Scraper's runs, which live only in memory today — with
trust and source kept on re-read. ADR-0071 (Proposed, stub).

**#023 — Forward limit forecast.** Project limit utilisation forward on the
plan world. The Takahashi–Alexander generation for plan-less funds shipped
2026-07-17 (ADR-0105); the forecast itself is the open half.

**#032 — Regulatory reporting pre-fill.** Pre-filled, plausibility-checked
drafts of the quarterly BerVersV returns for AnlV-regulated investors, from the
AnlV classification, NAVs and the limit engine already in place — a pre-fill,
not a system of record. Needs a book-value path and an asset-pool split.

**#045 — FX / asset-return attribution.** Split a functional-currency return
into asset performance and currency effect. ADR-0102 §1.

**#047 — Investor report.** Define the investor report as a product artefact —
content on the #046 hierarchy, periodicity, audience, commentary, branding,
format — before building it. Expected to converge with #001.

**#060 — Watch Desk Area switch.** Let a tenant switch the whole Watch Desk Area
off, not only its beat or single families (#069); needs its own ADR on what
happens to existing findings and cases.

**#068 — Tenant-local provider entries.** Tenant-maintained provider lists with
no directory involved; changes the provider channel's trust model. ADR-0129 §1.

**Shirley phone mode ("4L", low-latency loop layer).** A fluid spoken
conversation with Shirley: a low-latency conversation model carries the dialogue
and hands substantive questions to Shirley, who answers with her tools. Announced
on the website, concept stage, no build decision.

**Scenario regimes (from #034).** Timing beyond an immediate shock (ramp, lag,
decay), rate and duration shifts, spreads, default and recovery; named scenario
sets; access from Shirley and Irene.

**Synthetic unitisation (from #038).** Unitised valuation for private-markets
positions, specified and deferred in ADR-0097 §8.

**Watch Desk families (from #057).** Class-level price selectors, book-derived FX
pair sets, a configurable price direction, and a pacing family after #023.

**Market-data breadth (from #036).** FIGI persistence, the weight-DTO successor
ADR, a capability-based live-eligibility rule, Preqin and PitchBook adapters,
credentialed Bloomberg variants.

**Limits surface extras (from #019).** PDF export of the limits view and a
drill-down from asset class to investments.

**Course-hold assistance.** Irene assessing the consequences of a deviation
against the plan; a thinking record in
[`docs/concepts/course-hold-assistance.md`](concepts/course-hold-assistance.md),
not before #023.

---

## Won't-do

| ID | Title | Closed | Reason |
|---|---|---|---|
| #010 | Portfolio Review filter mechanism | 2026-07-11 | The review shows everything hierarchically instead (#046); the investor perspective is #047. |
| #037 | Provider credential management | 2026-07-29 | Superseded by #055. |

---

## Shipped

| ID | Title | Shipped | ADR |
|---|---|---|---|
| #002 | Report Scraper | 2026-05-19 | ADR-0053 |
| #004 | Shirley base migration | 2026-05-15 | ADR-0048 – ADR-0052 |
| #005 | Strategic Asset Allocation (Back Office) | 2026-05-17 | ADR-0054 |
| #006 | Asset-class name in the chart header | 2026-05-12 | — |
| #008 | Single-investment Portfolio Review | 2026-06-01 | ADR-0073 |
| #009 | Statistics latency fix | 2026-05 | ADR-0065 |
| #011 | Region model for country aggregation | 2026-05-13 | ADR-0046 |
| #012 | Benchmarks and attribution, phase 1 | 2026-05-25 | ADR-0061, ADR-0062 |
| #013 | Front Office overview KPI strip | 2026-05-29 | ADR-0067, ADR-0068 |
| #014 | Front Office overview charts | 2026-06-03 | ADR-0072 |
| #015 | Multi-user and permissions (roles, super-admin, tenant user management) | ≤ 2026-08-16 | ADR-0063, ADR-0064 |
| #016 | Qt sunset — web as the only surface | 2026-07-02 | ADR-0094 |
| #022 | Shirley: tool inventory in the system prompt | 2026-06-02 | — |
| #024 | Shirley: Back Office analysis tools | 2026-06-01 | ADR-0069 |
| #026 | Database reset and init scripts | 2026-05-12 | — |
| #027 | `db/init/` mechanism documented | 2026-07-11 | — |
| #028 | Language and privacy hygiene | 2026-05-10 | — |
| #029 | Image input for Shirley | 2026-06-04 | ADR-0075 |
| #030 | Voice input and output for Shirley | 2026-06-04 | ADR-0076 |
| #033 | Watch Desk and Irene | 2026-07-02 | ADR-0085 – ADR-0089 |
| #034 | Scenario analysis, v1 | 2026-07-17 | ADR-0104 |
| #038 | Position model | 2026-07-09 | ADR-0097, ADR-0098 |
| #039 | Migration round-trip test design | 2026-07-10 | — |
| #040 | Statistics and SAA currency contract | 2026-07-11 | ADR-0102 |
| #043 | Glossary: currency terms | 2026-07-11 | — |
| #048 | Cash as a first-class asset class | 2026-07-13 | ADR-0103 |
| #049 | Planning Desk and cash flow planning | 2026-07-23 | ADR-0104 |
| #050 | Price-movement watch family | ≤ 2026-08-16 | ADR-0116 |
| #051 | Cases | 2026-07-22 | ADR-0107 |
| #052 | AGPL public release | 2026-08-16 | — |
| #053 | UI polish pass | 2026-08-16 | — |
| #054 | CI, lint and type-check hardening | 2026-08-16 | ADR-0109, ADR-0110 |
| #055 | Scoped settings and credential vault | ≤ 2026-08-16 | ADR-0112 |
| #056 | Chart snapshot persistence | ≤ 2026-08-16 | ADR-0114 |
| #057 | Watchpoint registry and signal families | ≤ 2026-08-16 | ADR-0115, ADR-0116 |
| #058 | Built-in tick scheduler | ≤ 2026-08-16 | ADR-0117 |
| #059 | Per-tenant voice settings | ≤ 2026-08-16 | ADR-0118 |
| #061 | Transactions | 2026-09-08 | ADR-0128 – ADR-0130 |

---

## Appendix — legacy IDs

Older ADRs cite roadmap items by their pre-June-2026 IDs. They map as follows:
A1 → #001, A2 → #002, A3 → #003, A4 → #004, A5 → #005, A6a → #006, A6b → #007,
A7 → #008, A8 → #009, A9 → #010, A10 → #011, A12 → #012, A13 → #013,
A14 → #014, B1 → #015, B2 → #016, B3 → #017, B4 → #018, B5 → #019, B6 → #020,
B7 → #021, B8 → #022, B9 → #023, B10 → #024, C1 → #025, D1 → #026, D2 → #027,
D3 → #028 (there was no A11). Sub-item labels such as B1c or B5d refer to parts
of their parent item.

`#044` and `#062` were never issued: in both cases an accepted ADR had already
named its follow-up with the next number, and the ADR won.
