# PortfoliFLOW — Roadmap

**Status:** Active since the Phase-6 close-out
**Last updated:** 2026-08-21
**Owner:** Soenke (ProcessReengineer)

---

## About this document

This roadmap is the single steering document for PortfoliFLOW. It supersedes the
migration-era **A/B/C/D taxonomy** (Deep Dive / New Features / Infrastructure /
Quick Wins) and the retired Mission-Control chat pattern. Forward-looking work is
now organised into two active categories — **Loose ends** and **Features** — plus a
passive **Shipped** record that keeps the active lists lean.

The document is written in **English** (ADR-0008), removing the last German-language
documentation exception in the repository.

Each item is portioned so it can be the starting point of one implementation chat:
the roadmap line + a current Repomix snapshot + (optionally) a short concept
discussion = one focused session.

This restructure is **translation and re-organisation only**. It does not re-audit or
re-open any item state; the doc/code reconciliation pass of 2026-06-03 (ledger:
`docs/_audit/doc-code-reconciliation-2026-06-03.md`) remains the source of truth for
item states. Internal working documents referenced from this roadmap and from ADRs
(`docs/_archive/`, `docs/handover/`, `docs/_audit/`) are not part of the public
repository; the references are kept as historical pointers.

---

## Conventions

### ID scheme

Items carry a **flat, category-independent running number** (`#001`, `#002`, …). The
ID never encodes the category, so it survives an item moving between buckets,
splitting, or shipping. IDs were assigned once in old-category order (A-items, then
B, then C, then D; by old number), and are never reused.

**Next free ID:** `#062`.

> **`#044` is unissued and stays that way.** ADR-0102 was written against a next-free marker
> of `#045` and refers to the attribution follow-up by that number in four places (§1,
> §Rationale, §Alternatives, §Consequences). The ADR is filed and immutable, and this
> document's own ADR-discipline rule is that **the ADR wins** on conflict — so the item was
> raised as `#045` and `#044` was never issued to anything. It is a hole, not a lost item.
> Consistent with "IDs are never reused", it is not to be back-filled.

The old per-category IDs (A1, B5, C1, …) are preserved on each item as a `Formerly:`
alias and in the full crosswalk near the foot of this document. Inbound references in
the ADRs (`roadmap B9`, `roadmap A2/A3`, …) resolve through that crosswalk — the ADRs
are **not** rewritten. Sub-item labels (B1a–B1f, B2a/B2b, B5a–B5d) are documented
inside their parent item and are not renumbered.

### Status

- `open` — not yet begun.
- `in-progress` — currently in work (with a date).
- `blocked` — waiting on another item (with a reference).
- `done` — completed; moves to the **Shipped** record with its date and ADR.
- `won't-do` — closed without shipping (with a date). The decision and its reasoning
  are recorded on the item; the row stays in its bucket rather than moving to
  **Shipped**, which is a record of what was *built*. Not a synonym for deferred:
  a `won't-do` item is closed, not postponed.

### Priority

- `P1` — prerequisite for the first release, or for another P1 item.
- `P2` — substantial value, but not release-blocking.
- `P3` — polish, quality-of-life, or non-urgent extension.

Priority is **intrinsic importance**, not a deadline or tempo signal.

### Demo-path flag

- `Demo: yes` — on the 10 June demo click-path; must be stable.
- `Demo: no` — not on the demo walk.

This is a **stability/visibility** flag, not a tempo or priority signal. Every item was
`no` at the 2026-06-03 restructure, pending the operator's demo-walk pass (see the
operator-action note at the head of the change accompanying it). The first two `yes`
flags are **#048** and **#049** (2026-07-13): cash correctness is visible on every
surface that states an AUM figure, and the Planning Desk is a demo surface in its own
right. The remaining items are still `no` pending that pass.

### ADR discipline

Every consequential item references its ADR(s). On conflict, **the ADR wins**.

---

## Loose ends

Unfinished, inconsistent, provisional, or deferred states that must be resolved:
half-migrated surfaces, known gaps, cleanup, deferred technical debt, and the legacy
sunset. These are not "new" capabilities in the strict sense — they close parity with
the Qt surface or finish what is already partly built.

| ID | Title | Status | Prio | Demo | ADR |
|---|---|---|---|---|---|
| #001 | Portfolio Review full build-out (PDF export + detail areas) | open | P2 | no | ADR-0073 §5 |
| #003 | DD Support (Assistants) | open | P3 | no | — |
| #007 | Investment Sub-Class schema extension | open | P3 | no | — |
| #010 | Portfolio Review filter mechanism | won't-do (2026-07-11) | P3 | no | — |
| #025 | Hetzner Deployment | open | P1 | no | — |
| #031 | Liquid-archetype test-data fidelity follow-ups | open | P3 | no | ADR-0081 |
| #035 | DataStore complex decommission (Qt-sunset Stage 2) | open | P2 | no | ADR-0094 §5 |
| #041 | Functional-currency field renames (`*_eur`) | open | P3 | no | ADR-0099 §Follow-ups |
| #042 | Live FX-rate supply | open | P3 | no | ADR-0099 §5 |
| #048 | Cash as first-class asset class (ADR-0103 implementation) | shipped (2026-07-13) | P1 | yes | ADR-0103 |
| #052 | AGPL Public Release Track | shipped (2026-08-16) | P1 | no | — |
| #053 | UI Polish Pass (all eight Areas) | shipped (2026-08-16) | P1 | no | — |
| #054 | CI & Lint/Typecheck Hardening | shipped (2026-08-16) | P1 | no | ADR-0109, ADR-0110 |
| #056 | Chart Snapshot Persistence (session rehydration + case pinning) | in-progress (2026-08-05) | P1 | no | ADR-0114 |
| #058 | Built-in Tick Scheduler (in-process default, systemd opt-out) | open | P1 | no | ADR-0117 |

### #001 — Portfolio Review full build-out (PDF export + detail areas)

- **Formerly:** A1
- **Status:** open
- **Priority:** P2
- **Demo-path:** no
- **ADR:** ADR-0073 §5 (render target); ADR-0020 / ADR-0026 (reporting engine)
- **Dependencies:** Portfolio-Overview six-tile section (shipped, #013)

**The gap.** The shipped variant is a six-tile section (header KPI strip plus a 3×2
tile grid) for the Portfolio Overview. The full build-out turns it into an
automatically generated report the investor can pull at any time as a "how is my
portfolio doing" detail overview, exportable as PDF: detail areas (performance
attribution, vintage-year analysis, cashflow forecast, risk decomposition), branding
templates (logo, colour scheme, investor-specific tweaks), and a snapshot mechanism so
reports are historically reproducible rather than live-only.

**Resolution.** The render-target decision was already made under #008 (ADR-0073 §5):
Path B — server-side Plotly rendering via `kaleido` over the same bundle contract
(`SingleInvestmentReviewBundle` / `PortfolioOverviewBundle`), composed in a background
job into a PDF (one portfolio page, then one page per investment). The bundle
dataclasses are the shared seam; `kaleido` and a batched `get_full_review()` loader are
introduced only at this goal. PDF export itself stays `open`. Open design questions:
PDF vs. HTML-with-print-CSS (PDF is more robust for sending, print-CSS is closer to the
web stack and cheaper); server-side rendering vs. browser-driven via headless Chrome.

### #003 — DD Support (Assistants)

- **Formerly:** A3
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** #002 Report Scraper (same backend stack)

**The gap.** Sibling module to the Report Scraper, sharing the `ExtractionService`
backend. Processes GP due-diligence documents (PPMs, LPA drafts). Lower priority than
the Report Scraper because DD is rarer than recurring quarterly reports. Not yet on the
web surface.

**Resolution.** A web surface analogous to the Report Scraper, with PPM-specific
extraction templates (fund size, strategy, track record, key-person clauses).

### #007 — Investment Sub-Class schema extension

- **Formerly:** A6b
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** —

**The gap.** Completion of the naming polish. The Qt version additionally showed the
strategy (e.g. "Large Cap Defensive", "Small Cap Europe") in the article header. In the
Excel format this comes from row 3 of the `Attributes` sheet ("Investment Sub-Class")
and is currently *discarded* by the web investment extractor; the DB schema has no
corresponding field.

**Resolution.** Schema migration — either a new `investment_subclass: str | None`
column on `investments` (cleaner) or storage in the `type_specific_data` JSONB
(additive but an undefined type contract). Extend the Excel extractor to read the
sub-class row into the investment DTO; extend the charts route header to
`{Name}, {Asset-Class}, {Sub-Class}`; carry the single-investment surface along if
comparable headers exist there; Alembic migration with a backfill strategy (existing
investments get `NULL`, new imports fill the field).

### #010 — Portfolio Review filter mechanism

- **Formerly:** A9
- **Status:** won't-do (2026-07-11) — superseded by **#046** and **#047**
- **Priority:** P3
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** Portfolio-Overview section (shipped, #013)

**The gap.** A filter on the Portfolio-Overview section so the aggregation covers only
a selected subset of investments (e.g. "show only private equity"). Intended to become
a core mechanic for several investor views. The backend already exists:
`PortfolioReviewService.get_portfolio_overview` already accepts the keyword filter
`investment_ids: list[UUID] | None`
(`services/portfolio_review/portfolio_review_service.py:217`) and passes it into the
aggregation. The open remainder is therefore **only** filter UI + route wiring + URL
reflection — no further backend work.

**Resolution.** Filter UI in the section form (alongside the as-of-date form); filter
dimensions (investment type, asset class, manager, vintage-year range) resolved to the
`investment_ids` list the service already accepts; route wiring to pass the resolved
ids through; URL reflection so bookmark/share works; an empty-state on an empty
selection.

**Resolution (won't-do, 2026-07-11).** The filter *interaction model* is rejected: the
Portfolio Review will not offer subset selection. Instead of letting the reader narrow the
report to a chosen slice, the review shows **everything, hierarchically** — portfolio
aggregate, then per sub-asset class, then per investment — which is the successor item
**#046**. The investor-report perspective this item was "intended to become a core mechanic
for" is not dropped either; it is now explicitly its own item, **#047**, rather than an
unstated motivation hiding inside a filter feature.

The already-existing backend filter parameter stays: `PortfolioReviewService.get_portfolio_overview`
continues to accept `investment_ids: list[UUID] | None`, unused by the review UI but
available to future callers (#046's per-class and per-investment tile sets are the obvious
ones). **Removing it is explicitly not part of this decision** — what is rejected is the
filter as a user-facing interaction, not the service API that would have backed it.

### #025 — Hetzner Deployment

- **Formerly:** C1
- **Status:** open
- **Priority:** P1
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** #015 Multi-User, #016 Qt Sunset (so only the web stack is deployed)

**The gap.** Production deployment pipeline on a Hetzner server. Goal: first real use by
non-Soenke users (pre-launch phase). This is operational groundwork, not a user-visible
feature; it sits under Loose ends as a release-blocking known gap. *(Old Category C —
Infrastructure — has no direct equivalent in the two-bucket model; flagged for the
operator in the accompanying report.)*

**Resolution.** Server setup (OS hardening, reverse proxy via Caddy or nginx,
Let's-Encrypt certificates); app deployment (Docker Compose vs. systemd — concept
decision needed); Postgres production hardening (nightly `pg_dump` + WAL archiving,
restore test, connection pooling); secrets management (`.env` files to start, a secret
store long-term); structured (JSON), centralised logging; monitoring (at least a
healthcheck endpoint, ideally Prometheus/Grafana or an uptime-robot equivalent); CD (push
to `main` → build → deploy, GitHub Actions or a self-hosted runner; the CI
half — lint / typecheck / tiered test execution — is **#054**). Open design
questions: container vs. systemd; subdomain (`{tenant}.portfoliflow.com`) vs.
path-based (`portfoliflow.com/{tenant}/...`) multi-tenant routing.

**Not a #052 gate (2026-07-29).** The AGPL release is a repository flip; the
hosted instance is a parallel track at unchanged priority. Only the AGPL-§13
source-availability link in the running application (Chat B / #052 gate 4)
touches the release.

### #027 — Document the `db/init/` mechanism

- **Formerly:** D2
- **Status:** done (2026-07-11)
- **Priority:** P3
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** —

**The gap.** Lesson #7 from Block 1: a volume drop is required for a full DB reset,
because `db/init/01-create-app-role.sql` runs only on the first container start.

**Resolution (shipped 2026-07-11).** Documented in place rather than in a new
`docs/development.md` — `db/README.md` is where the reader already is when the question
arises, and it owns the surrounding context (roles, migrations, reset). It gained a
**`## The init/ directory`** section covering, in order: what the SQL actually does (the
unprivileged `portfoliflow_app` role, plus `CONNECT` / `USAGE` and the
`ALTER DEFAULT PRIVILEGES` grant that makes Alembic-owned tables reachable without a
per-migration `GRANT`); **when it runs** — executed by the Postgres entry point from
`/docker-entrypoint-initdb.d` (mounted by `compose.yml`) exactly once, on the first start
against an **empty** data volume, never again, which is why a role change on a live dev DB
needs either manual SQL or a full reset; **how to re-trigger it** — `scripts/db-reset.sh`
(volume teardown → fresh init → `alembic upgrade head` → bootstrap), explicitly contrasted
with `portfoliflow reset-dev --confirm`, which truncates data on the existing volume and
re-runs nothing; and the **division of labour** — `init/` owns cluster-level bootstrap
(roles) only, Alembic owns all schema. `docs/architecture.md`'s `db/` paragraph gained one
sentence carrying the never-re-runs property and pointing at `db/README.md`.

### #031 — Liquid-archetype test-data fidelity follow-ups

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** ADR-0081 (Consequences → Neutral / Follow-ups)
- **Dependencies:** ADR-0079 / ADR-0080 data layer (merged); the v26 workbook +
  importer extension (ADR-0081, later prompts)

**The gap.** Refinements deferred from ADR-0081's **Variante-A** choice (preserve the
existing daily NAV path as the ex-income price NAV and add income flows on top, rather
than regenerating NAVs with ex-distribution drops). Variante A keeps portfolio scale
(≈ €1.08 bn) and the two intentional SAA breaches invariant by construction, at the
cost of a handful of accepted imprecisions in the shipped sample data. Each is a
concrete future action:

1. **Duration-identity reconciliation of the shipped dataset.** The preserved NAV path
   has no ex-distribution drops on income pay dates, so reconstructed total return runs
   marginally "hot". Regenerate the showcase bonds via a full TR-index split
   (Variante B) and re-verify the two SAA breaches computationally. ADR-0079 Test 1
   (≤ 1e-6 reconciliation) stays satisfied in the interim by separate unit-test
   fixtures from the pure generator, not by the shipped data.
2. **Dedicated cash treatment.** Replace the stopgap of modelling Cash (Investment T)
   as `listed_bonds` with a degenerate Fixed-Income profile by a first-class cash
   treatment (balance plus running yield, without a duration / OAS / rating ladder).
3. **Re-examine the `Credit` / `Cash` aliases against real GP data.** The two alias
   additions (`credit`, `cash` → `listed_bonds`) bend the alias-table discipline
   ("extend only on real input"); revisit them once real manager labels are available.
4. **Historise equity sector/region composition.** Equity composition currently ships
   as a single snapshot (anchored to the latest actual NAV date, no drift). Extend the
   same long-format, time-series mechanism (ADR-0080) to it so equity composition drift
   is captured like the Fixed-Income weight ladders.
5. **Resolve the legacy per-investment view's interim incoherence.** Listed instruments
   now carry income inflows but no capital calls, so the old Capital-Account /
   single-investment review computes NaN multiples for them (currently guarded). Close
   this once the ADR-0079 §1 archetype routing is built and listed funds render the
   Total-Return / Fixed-Income surface instead of the private-markets multiples.

**Resolution.** Address the five points incrementally as the liquid-archetype surfaces
mature; none is release-blocking. Cross-reference ADR-0081.

### #035 — DataStore complex decommission (Qt-sunset Stage 2)

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** open
- **Priority:** P2
- **Demo-path:** no
- **ADR:** ADR-0094 §5 (Stage-2 scope; a follow-up ADR is the first deliverable)
- **Dependencies:** Category-A review item **A5** (`load_excel` relocation, 2026-07-02
  handover — not to be confused with roadmap crosswalk A5); **#001** (Portfolio Review
  full build-out — its Bundle-based PDF render path confirms the DataStore `ReportEngine`
  has no future consumer)

**The gap.** Stage 1 of the Qt sunset (ADR-0094) removed the `gui/` surface but
deliberately left the in-memory **DataStore complex** in place, because it still has live
web-side consumers. `core/data_store.py` and `core/persistent_data_store.py` remain; the
DataStore-coupled `services/reporting/report_engine.py` + `ProviderContext` path is still
wired; and three module shells (`modules/back_office/saa.py`,
`modules/front_office/data_import.py`,
`modules/investor_communication/portfolio_review.py`) still touch it. Two couplings block
a blind deletion: `load_excel` in `data_import.py` is consumed by the web upload route,
and `compute_irr` in `services/reporting/data_providers/_calculations.py` is consumed by
the live web analytics — so `_calculations.py` survives regardless.

**Resolution.** A follow-up ADR scopes the removal once its prerequisites land: relocate
`load_excel` off the DataStore path (review item A5), and confirm via #001 that the
Bundle-based PDF render path leaves the DataStore `ReportEngine` with no future consumer.
Then retire `core/data_store.py`, `core/persistent_data_store.py`, the DataStore-coupled
report engine and `ProviderContext`, the orphaned
`modules/investor_communication/portfolio_review.py`, and the DataStore usage in
`modules/back_office/saa.py`; drop the `data_store_entries` table by forward migration
(schema history is immutable); and re-evaluate the `matplotlib` / `squarify` dependency
question at the same time. Cross-reference ADR-0094 §5.

### #039 — Migration-roundtrip test design

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** done (2026-07-10)
- **Priority:** P3
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** none

**The gap.** The five migration-roundtrip guards —
`tests/regression/test_b021_ingest_origin_roundtrip.py`,
`test_b022_market_data_schedule_roundtrip.py`,
`test_b024_position_model_roundtrip.py`, `test_b025_system_origin_roundtrip.py`, and
`test_liquid_archetype_migration_roundtrip.py` — each run `alembic downgrade -1`
**relative to head** rather than against the revision they are named for. Every newly
added migration therefore re-targets all of them, breaking the previous migration's
roundtrip test — observed when adding `b025` broke `b024`'s guard.
Worse, a failed run leaves the compose Postgres sitting at the downgraded revision, so
the subsequent RLS tests fail for a schema reason that has nothing to do with them —
false failures that cost real triage time and are only cleared by a manual
`alembic upgrade head`.

**Resolution (shipped 2026-07-10, commit `5599175`).** Each roundtrip test is now pinned to
its own revision: it downgrades to a **named** `_BELOW` target by explicit revision
identifier, never relative to head, and restores `alembic upgrade head` in a `finally` block
so a failure cannot cascade into the rest of the suite. Deferred out of #038 (2026-07-09),
where the `b025`-breaks-`b024` instance was observed; the pattern was first established by
`tests/regression/test_b026_fx_rates_roundtrip.py` and
`tests/regression/test_b027_cash_type_roundtrip.py` (multi-currency programme), and the
Multi-Currency Block 1 commit converted all five listed offenders to the same idiom:
`test_b021_ingest_origin_roundtrip.py` (→ `b020`),
`test_b022_market_data_schedule_roundtrip.py` (→ `b021`),
`test_b024_position_model_roundtrip.py` (→ `b023`),
`test_b025_system_origin_roundtrip.py` (→ `b024`), and
`test_liquid_archetype_migration_roundtrip.py` (→ `b015`). Adding a new migration no longer
re-targets — and so no longer breaks — the previous migration's guard.

### #040 — Statistics / SAA currency contract

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** done (2026-07-11)
- **Priority:** P2
- **Demo-path:** no
- **ADR:** **ADR-0102** (the successor decision that closes this item); ADR-0099 §6 (named
  out of scope there — this item is where the follow-up lived); ADR-0099 §4 (the conversion
  seams it had to be consistent with)
- **Dependencies:** the ADR-0099 §4 conversion boundary (landed, multi-currency Block 3)

**The gap.** ADR-0099 §4 converts NAV and cashflow frames into the tenant's functional
currency at exactly one seam — the `PortfolioReviewService` load path — and ADR-0099 §6
explicitly leaves the statistics / SAA / archetype layer on its previous behaviour. So
`services/portfolio_analysis/` still measures **nominally** across position currencies: the
current-portfolio weights (`PortfolioAnalysisService._latest_nav_weights`) sum a USD fund's
latest NAV into the total as if it were functional currency, and the return statistics
feeding the efficient frontier are computed on unconverted series. Since Block 3 the two
halves of the app therefore visibly disagree on any mixed-currency book — the Review KPIs
and the Front-Office Overview are converted, the Portfolio-Analysis weights and statistics
are not — including on the shipped v31 sample data (Fund B in USD, Fund C in GBP, plus the
`Cash USD` position).

**Resolution.** Define the layer's currency contract explicitly and make it consistent with
the ADR-0099 §4 seams: measurement in the functional currency, using the same point-in-time,
carry-forward conversion the review seam already applies. ADR-0099 §6 names the substantive
question this must settle — total-return statistics differ between **local-currency** and
**functional-currency** measurement for foreign-currency positions, and a frontier optimised
on one is not the frontier of the other — so the contract has to state which measurement the
frontier optimises and how that choice is disclosed on the surface. Likely a successor ADR
(ADR-0099 records this as a deliberate follow-up, not a defect); no engine rewrite is
expected beyond routing converted frames into the analysis service.

**What landed (2026-07-11, ADR-0102).** The successor ADR was written and accepted, and the
seam work shipped. The substantive question is settled: **measurement is in the functional
currency, FX effect included** (ADR-0102 §1) — the same choice ADR-0099 §4 already made for
the review path's IRR/TVPI/DPI, so the frontier is optimised on functional-currency returns,
volatility and the correlation matrix are computed on them, and levels (totals, weights,
composition, composite NAV weights) become consistent with Review/Overview by construction.
The three services — `PortfolioAnalysisService`, `StatisticsService`,
`BenchmarkComparisonService` — convert at the **existing** ADR-0099 §4 boundary through the
same `build_portfolio_fx_converter` / `convert_series` idiom (callers added to the seam, not
a variant of it), each taking `TenantRepository` + `FxRateRepository` and building one
converter per request. **Analytics purity is untouched** — no function under
`services/analytics/` changed, and `test_analytics_layer_pure.py` stays green. **Error
states:** the three section routes (`web/routes/statistics.py`,
`web/routes/portfolio_analysis.py`, `web/routes/benchmarks_attribution.py`) catch
`MissingFxRateError` and render an HTTP-200 error partial for the HTMX section swap
(`statistics_error.html`, `portfolio_analysis_error.html`,
`benchmarks_attribution_error.html`), on the `overview_error.html` / `limits_error.html`
precedent — still no silent 1:1 anywhere. **Invisibility preserved:** a single-currency
tenant hits `build_portfolio_fx_converter`'s zero-read fast path, every conversion is an
identity pass-through, and each section carries a regression test mirroring
`test_single_currency_tenant_sees_no_fx_surfaces` — byte-for-byte unchanged output. The two
halves of the app now agree on any mixed-currency book (the v31 sample data included), which
was the demo failure this item existed to remove. **Deliberately not built:** the
local-currency view — decomposing a functional-currency return into asset-performance and
currency components is attribution, an additive capability, raised as **#045** rather than
bolted on as a parallel measurement basis (ADR-0102 §1, Alternatives Option C). **#041**
remains open and proceeds afterward, against the converted services.

### #041 — Functional-currency field renames (`*_eur`)

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** ADR-0099 §Follow-ups (the rename, deliberately not bundled); ADR-0101 §3 (labels
  made correct **without** it); ADR-0044 (rename precedent)
- **Dependencies:** none — but best run standalone, with no multi-currency block in flight

**The gap.** Since Block 3 every aggregate the system reports is in the tenant's **functional
currency**, but the names still say EUR: `aum_eur`, `nav_eur`, `cash_eur` on the DTOs, the
template context keys derived from them, and the `portfolio_aum.aum_eur` column itself. The
values are right, and since ADR-0101 §3 the **labels** are right too (`_format_money_compact`
threads `functional_currency` from the bundle into the money labels, so a non-EUR tenant is
no longer mislabelled `€`) — only the identifiers still lie. Both ADR-0099 §Follow-ups and
ADR-0101 §Alternatives record the deferral deliberately: bundling a cross-cutting rename into
a UI block violates the one-concern rule, and labels can be correct without the rename.

**Resolution.** One coordinated rename refactor, `*_eur` → `*_functional`, across the DTO
fields, the route/template context keys built from them, and the `portfolio_aum.aum_eur`
column by **forward** migration (schema history is immutable — the same posture as #035's
`data_store_entries` drop). ADR-0044 is the naming/rename precedent. Carry no behavioural
content in the same change, so the diff reviews as pure renaming.

### #042 — Live FX-rate supply

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** ADR-0099 §5 ("Live (deferred, enabled)"); ADR-0091 (provider port, DTO, capability
  matrix — the seams it rides on); ADR-0092 (Excel precedence, already encoded)
- **Dependencies:** #036 Live Data Import (the port / capability / adapter machinery)

**The gap.** FX rates enter the system through exactly one path: the Excel `FX rates`
market-reference sheet (Block 2, ADR-0099 §5). The live half is designed but unbuilt — there
is no `SeriesKind.FX_RATE`, no `fx_rate` capability-matrix entry, and no adapter. A
multi-currency tenant therefore maintains rate coverage by hand, and an uncovered
(currency, date) fails loudly by design (`MissingFxRateError`) rather than falling back to
1:1. The landing zone is already waiting: `FxRateRepository.upsert_live` exists and is
**dormant** — it carries the ADR-0092 precedence semantics (a live write never overwrites an
`'excel'` or `'manual'` row, only refreshes its own prior `'live'` rows) and has no caller.

**Resolution.** Extend the ADR-0091 seams: add the `fx_rate` kind to `SeriesKind`, add an
`fx_rate` capability entry, and write the adapter. **ECB SDMX** is the preferred source per
ADR-0099 §2 — keyless, revision-stable, supervisory-grade, and published as EUR/XXX pairs, so
an EUR-reference deployment needs no transformation; Yahoo `EURUSD=X`-style tickers are a
possible interim. One design question is real and should be settled up front: `NormalizedSeries`
is keyed on a `NormalizedIdentifier` (a security scheme/value pair), and a currency is not a
security — so the DTO's keying for a rate series needs a decision, a sibling of the
bucket-dimension gap the **weight-DTO successor ADR** already owes under #036. Beyond that,
ADR-0099 §5 predicts **zero downstream change**: the port, capability matrix, and the dormant
`upsert_live` absorb the new producer, exactly as the Bloomberg adapter did under #036.

**The stakes rose (2026-07-13, ADR-0104 §3).** This item stays **deferred at P3**, but its value
increased and the note is worth carrying. The Planning Desk builds its plan-world FX path by
carry-forward held flat, over the **same** rate frame — so on a multi-currency tenant, an
uncovered (currency, date) pair no longer merely fails a background conversion, it **blocks the
plan world loudly and user-visibly**: `MissingFxRateError` surfaces on the Planning Desk itself.
That is the designed behaviour, and correct — there is still no silent 1:1 fallback anywhere —
but it raises the **operational** bar on workbook FX-sheet completeness, which is exactly the
manual burden an ECB SDMX adapter removes. Deferral unchanged; the argument for closing it is
now stronger than "the rates are typed in by hand".

### #043 — Glossary v3: currency terms

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** done (2026-07-11)
- **Priority:** P3
- **Demo-path:** no
- **ADR:** ADR-0100 §Follow-ups (raises it); ADR-0099 §Follow-ups (the three currency terms);
  ADR-0002 / ADR-0084 (the glossary lineage a v3 section extends)
- **Dependencies:** none — documentation-only

**The gap.** The multi-currency programme introduced five terms that are now load-bearing in
the code and normatively defined in the ADRs, but have **no canonical glossary entry** in
`CLAUDE.md` or `docs/architecture.md`. ADR-0099 §Context distinguishes **functional currency**
(the portfolio's reporting currency, per tenant), **position currency** (the currency an
investment is denominated and settled in), and **reference currency** (the base of the
FX-rate dataset — a property of the *data*, not of the portfolio; the two may coincide but are
distinct). ADR-0100 adds the pair **explicit cash position** (an `investment_type = 'cash'`
row in a non-functional currency) versus **cash residual** (`aum_total − Σ nav_functional`,
now narrowed to the functional-currency float). Both ADRs name the glossary gap as an explicit
follow-up. Until it is closed, prompts and prose can conflate the three currencies — the exact
failure mode the glossary discipline exists to prevent.

**Resolution (shipped 2026-07-11).** Closed by the 2026-07-11 doc/code reconciliation pass
(ledger: `docs/_audit/doc-code-reconciliation-2026-07-11.md`). The `CLAUDE.md` glossary — the
canonical carrier, per the ADR-0084 v2 precedent — gained the five terms as **glossary v3**:
**functional currency**, **position currency**, **reference currency**, **explicit cash
position**, and **cash residual**, each cited from the ADR sections that define them
(ADR-0099 §Context / §2 for the three currencies; ADR-0100 §§2–3 for the cash pair) rather
than re-derived. The `AUM` row was reconciled in the same pass — its "cash is the residual,
not a persisted entity" claim predates ADR-0100 and would otherwise contradict the new
**explicit cash position** row. `docs/architecture.md` carries the matching schema-vocabulary
and schema-narrative updates.

### #048 — Cash as first-class asset class (ADR-0103 implementation)

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** **shipped 2026-07-13** (see Shipped record)
- **Priority:** P1
- **Demo-path:** **yes** — AUM correctness on every surface that states an AUM figure
- **ADR:** **ADR-0103** (Accepted 2026-07-13)
- **Dependencies:** **#038** Position model (strands S0–S5 landed 2026-07-09 — the
  substantive dependency is satisfied; the item stays `in-progress` only for the operator
  walkthrough and the deferred synthetic unitisation); the multi-currency programme
  (ADR-0099–0102, shipped)
- **Blocks:** **#049** (Planning Desk build-out), **#034** (Scenario Analysis), and the final
  resolution of #038's blocked-by note

**The gap.** Cash is split across two representations. Foreign-currency balances are explicit
`investment_type='cash'` rows (ADR-0100, `valuation_mode='reported'`); the functional-currency
float is a **residual** computed over the imported `portfolio_aum` series (ADR-0055, narrowed
by ADR-0100 §3). Two mechanisms for one asset class means every engine either special-cases
cash or silently gets a different answer depending on which currency it is held in — and the
residual is only as good as the AUM series someone maintains by hand.

**Resolution.** ADR-0103 unifies the two: **all** cash becomes explicit, **unitised** with
stored unity prices (`price ≡ 1.0000` — the constraint #038 already pinned for exactly this
successor), statement-fed through a new workbook **Cash sheet** (format **v32**) with ADR-0060
carry-forward across the gaps between statements, `flow_type='investor_flow'` added as the
**eighth** canonical member, a **materialised cash plan path**, the efficient-frontier cash
exclusion pinned at the assembly seam, and **`portfolio_aum` retired by forward migration**
(schema history is immutable — the #035 / #041 posture). The maintenance objection ADR-0100
rejected this on dissolves at statement frequency: nobody maintains a daily series, the tenant
types in what the bank statement says.

**Ordering is load-bearing.** The `portfolio_aum` drop executes **strictly after** cash
materialises correctly and one reconciliation cycle (imported statement balances vs. the
retiring residual) has passed — the annex §A.3 ordering, sequenced inside the strand and
surfaced in its report. Consequence inventory: **39 files** (ADR-0103 §7) — the widest of the
programme so far, which is why it is one strand and its own implementation chat (kickoff
prompt of 2026-07-13). Migrations are claimed at implementation time, not reserved here.

**Shipped 2026-07-13.** All seven sub-strands landed: unitised cash (`b027`/`b029`), the v32
Cash sheet and statement-to-ledger derivation, `investor_flow` as the eighth `flow_type`
(`b028`), the materialised cash plan path, the frontier cash exclusion at the assembly seam,
and — as the final, gated step — **`portfolio_aum` retired by migration `b030`** after the
annex §A.3 reconciliation cycle passed. AUM is now `Σ nav_functional(t)` over the whole book,
cash included, with one shared formulation in `services/investments/aum.py` that the Overview
hero, the limit-coverage denominator and the `AUM`-sheet reconciliation control all resolve
through. The cash residual and its negative-suppression rule are retired (ADR-0103 §2).

*(Filed under Loose ends: this finishes what ADR-0100 deliberately built only half of — the
"make all cash explicit and demote the residual" option it rejected **for v1** — rather than
adding a capability. Its user-visible consequences are correctness, not new surface.)*

### #052 — AGPL Public Release Track

- **Formerly:** — (new; raised 2026-07-29)
- **Status:** shipped (2026-08-16)
- **Priority:** P1 (this item *defines* the first release)
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** #053 (gate 7), #054 (gate 5); the licensing apparatus and the
  flake-fix closure are tracked as gates here directly

**The gap.** The AGPL public release is a sequence of dependent, partly
non-reversible steps spread across four work streams (licensing texts, CI, flake
fixes, UI polish) plus two operator-side legal actions (logo, trademark). Nothing
ties the sequence together; the two hard ordering constraints — the trademark
filing must precede or coincide with publication, and the CLA must be in place
before the first external contribution is accepted — live only in session notes.

**Resolution.** One umbrella item carrying the ordered release-gate checklist.
Each gate is ticked by the closing chat/operator action; the repository flip
(gate 8) is legal only when gates 1–7 are ticked.

- [x] 1. Logo finalized (operator/design — blocks the word-image mark; the word
      mark is not blocked by it) — done
- [x] 2. EUIPO similarity search (PortfoliFLOW + Happy Computer Collective) — done
- [x] 3. Trademark filing — word marks **PortfoliFLOW** + **Happy Computer
      Collective**, Nizza 9/42/36 (**must precede or coincide with
      publication**) — done
- [x] 4. Licensing apparatus merged (Chat B: LICENSE [AGPLv3], CLA, CONTRIBUTING,
      TRADEMARKS, README licensing section) — done. **CLA before any external
      contribution — non-reversible ordering.**
- [x] 5. CI green on `main` (#054, Chat C) — done
- [x] 6. Flake-fix closure (Chat D) — done
- [x] 7. UI-polish closure (#053, Chat E) — done
- [x] 8. Repository flip to public — with the first public commit (2026-08-16)

**Seam to #025.** The code release is a repository flip; the hosted instance is a
parallel track and **not** a gate here. Only the AGPL-§13 source-availability
link in the running application (part of the Chat-B apparatus, gate 4) touches
the release.

### #053 — UI Polish Pass (all eight Areas)

- **Formerly:** — (new; raised 2026-07-29)
- **Status:** shipped (2026-08-16)
- **Priority:** P1 (blocks #052 gate 7)
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** blocks #052 (gate 7)

**The gap.** The repository flip makes every template public; the eight Areas
have never had one systematic visual/consistency pass as a whole.

**Resolution.** One systematic pass over all eight Areas in three steps: a
findings register (walk every surface, record cosmetic/consistency findings),
implementation, gate review against a fresh snapshot. **Functional findings
become new roadmap items** rather than growing this pass; **#046 (Portfolio
Review restructure) stays out** — it is a structural rebuild, not polish.
Findings taxonomy and register format are decided in the item's own chat
(Chat E).

**Shipped (2026-08-16).** Closed with the public release; gate evidence in the
#052 checklist.

### #054 — CI & Lint/Typecheck Hardening

- **Formerly:** — (new; raised 2026-07-29)
- **Status:** shipped (2026-08-16)
- **Priority:** P1 (blocks #052 gate 5)
- **Demo-path:** no
- **ADR:** ADR-0109 (lint, typecheck and CI contract), ADR-0110 (typing
  island set at CI landing — supersedes ADR-0109 §3 in part)
- **Dependencies:** blocks #052 (gate 5)

**The gap.** A public repository without visible, passing CI undermines the
trust argument the release exists to make. Today the full suite runs only on
the operator's machine; there is no ruff/format enforcement and no typecheck
in any automated path.

**Resolution.** Ruff rule set + format enforcement; GitHub Actions with tiered
test execution (fast tier on every push, full tier where affordable); typecheck
islands (strict where the codebase already sustains it, expanding). Detail —
rule selection, tier cut, island map — is decided in the item's own chat
(Chat C). **Seam to #025:** this item owns CI (lint / typecheck / test
execution); deployment/CD (push-to-`main` → build → deploy) stays in #025.

**Shipped (2026-08-16).** Closed with the public release; gate evidence in the
#052 checklist.

**Post-release typing note.** Four follow-ups are deliberately deferred past
the release by ADR-0109/ADR-0110 and collected here, in the order the ADRs
assign them:

1. **Analytics into the typing island set** (ADR-0110 §1 — the note's *first*
   entry). `services/analytics/` left the island set at CI landing because
   basic-mode pyright measured **163** findings there, every one an artefact
   of pyright inferring pandas types from source rather than a real defect.
   It re-enters via `pandas-stubs`. Measured with
   `pandas-stubs==3.0.3.260530` against pandas 3.0.2: analytics drops
   163 → 10, but `services/overlay/` re-opens 0 → 4. **Target:** analytics
   included at zero findings, with the overlay regression fixed in the same
   work item — the two are one piece of work, not two.
2. **Staged-adoption `ignore` removal** (ADR-0109 §1, §Consequences). The
   rules over the >50-hit threshold at adoption time, staged in
   `[tool.ruff.lint] ignore` with one comment per entry. That list — not the
   ADR — is the authoritative inventory. **Not** the DataStore
   per-file-ignores block (`core/data_store.py`, `modules/**`,
   `services/reporting/**`): that rides **#035**, since the DataStore complex
   is scheduled for decommission rather than modernisation (ADR-0094 §5).
3. **Whole-tree strict typing** (ADR-0109 §3). The island set expands package
   by package; strict mode across the tree is the end state, not a next step.
4. **Coverage revisit** (ADR-0109 §5). `pytest-cov` and a coverage badge were
   *declined* for the release — guard-based enforcement over percentage
   optics. Reopening the question belongs here.

**Two open CI-contract items that are *not* part of that note**, so the list
above is not mistaken for the complete follow-up set: the `full-suite.yml`
nightly cron is activated with the public flip (**#052 gate 8**) because
GitHub Actions minutes are metered while the repository is private
(ADR-0109 §4); and the rerun-once exception for the two known flake
signatures — migration-roundtrip downgrade, AI-service singleton pollution
in combined runs — is temporary by construction, deleted by the flake-fix
closure (**Chat D, #052 gate 6**) as a comment change, not an ADR amendment.

### #056 — Chart Snapshot Persistence (session rehydration + case pinning)

- **Formerly:** — (new; raised 2026-08-05)
- **Status:** in-progress (since 2026-08-05)
- **Priority:** P1
- **Demo-path:** no
- **ADR:** ADR-0114 (chart snapshot persistence); ADR-0048 (spec artefact),
  ADR-0050 (in-memory history — the deferral this item discharges),
  ADR-0107 §7 (pin anatomy)
- **Dependencies:** none

**The gap.** Shirley's charts do not survive navigation. The Plotly spec
reaches the browser as an SSE `chart` event and is deliberately stripped
before the LLM-bound tool message, so it never enters the session history:
switching area and back — or reloading the tab — restores the prose bubbles
only, and every chart of the session is gone. ADR-0050 records this openly
("Charts cannot be rehydrated") and defers the fix to an artefact-rehydration
strand. On the Cases side the same absence bites harder: a PM can pin a *text*
excerpt of a consultation (ADR-0107 C6) but not the figure the analysis
actually is, so reopening the case shows the words around a chart that no
longer exists.

**Resolution.** ADR-0114: charts are persisted as **frozen snapshots of the
rendered spec** — no replay, no recomputation, no silent refresh on any
surface, extending the C5 scenario-snapshot principle to chart artefacts. Two
seams, one artefact format: (1) a **per-session sidecar** beside the ADR-0050
history — never inside it, so the spec never re-enters the model's token
stream — captured in the SSE `chart_artifact` branch, sharing the history's
lifecycle, bounds and turn-group trim, and interleaved at its message
positions by `GET /chat/history`; (2) **`chart_snapshot`**, the fourth pin
artefact class after `document`, `scenario_snapshot` and `consultation`,
transporting the sidecar `artifact_id` **by reference** while the server
embeds the resolved spec in the journal payload, so the case record is
self-contained. A single **1 MiB `_CHART_SPEC_BYTE_CAP`** at the capture point
guards memory in the `_DATA_ROW_CAP` tradition: an oversized spec is not
archived, rehydration renders a calm placeholder, and the live render is never
refused. **No new table and no migration** — the chat seam is in-memory
(ADR-0050 migration trigger 2 unchanged; when it fires, the sidecar migrates
with the messages), the case seam reuses the existing journal-entry JSONB.

Filed under **Loose ends** because it finishes what ADR-0050 explicitly
deferred rather than adding a capability. Sequenced before the AGPL public
release (#052 family) and covered by the pre-release hardening test.

### #058 — Built-in Tick Scheduler (in-process default, systemd opt-out)

- **Formerly:** — (new; raised 2026-08-11)
- **Status:** open
- **Priority:** P1
- **Demo-path:** no
- **ADR:** ADR-0117
- **Dependencies:** — (orthogonal to the Watch Desk programme #057, P3–P6)

**The gap.** Both periodic jobs (Irene heartbeat, market-data live import)
require external systemd timers — unavailable or unwanted in many
self-host deployments. Per-tenant cadence is already data (ADR-0085/0086);
only the tick source needs a zero-configuration default.

**Resolution.** Per ADR-0117: an in-process asyncio tick task in the web
lifespan as the default tick source; a shared engine-parametrised tick
runner (the CLI ticks become thin wrappers); the cross-tenant due reads
sanctioned as audit-engine path 5 (read-only, schedule tables only);
environment-scope configuration (`TICK_SCHEDULER_ENABLED`,
`TICK_SCHEDULER_INTERVAL_SECONDS`); health/Super-Admin visibility instead
of UI editability. The systemd units under `docs/deploy/` remain as the
documented opt-out. Implementation prompts S2–S4.

---

## Features

New user-visible capabilities that did not exist in the Qt version or go substantially
beyond it. These are genuine product extensions, not migration close-out.

| ID | Title | Status | Prio | Demo | ADR |
|---|---|---|---|---|---|
| #015 | Multi-User & Permissions | in-progress (2026-05-26) | P1 | no | ADR-0063, ADR-0064 |
| #017 | Shirley: Code Generation & Auto-Execution | open | P2 | no | — |
| #018 | Shirley: Full App Control | open | P3 | no | — |
| #019 | Investment-Limit Monitoring (Anlagegrenzen, Phase 7) | in-progress | P2 | no | ADR-0055–0057, ADR-0083 |
| #020 | Shirley: Analysis Reads Phase 2 | open | P2 | no | ADR-0070 (stub) |
| #021 | Persistent Analysis-Result Storage | open | P2 | no | ADR-0071 (stub) |
| #022 | Shirley: System-Prompt Grounding | in-progress | P2 | no | — |
| #023 | Cashflow / Exposure Projection (Forward Limit Forecast) | open (TA slice shipped 2026-07-17) | P2 | no | ADR-0105 (TA slice) |
| #032 | Regulatory Reporting Pre-Fill (BaFin / BerVersV quarterly returns) | open | P2 | no | — (concept ADR at kickoff); ADR-0074 (scope) |
| #033 | Watch Desk (proactive heartbeat / Irene) | in-progress | P2 | no | ADR-0085–0089 |
| #034 | Scenario Analysis (economic downturn / rate / stress) | v1-scope shipped (2026-07-17); ADR-E open | P2 | no | ADR-0104 (v1); ADR-E (pending) |
| #036 | Live Data Import (provider-agnostic market-data ingest) | in-progress | P2 | no | ADR-0090–0093 |
| #037 | Provider Credential Management (per-tenant market-data credentials) | won't-do (2026-07-29) — superseded by #055 | P1 | no | ADR-0095 |
| #038 | Position Model (transactions, holdings, unitised valuation) | in-progress | P1 | no | ADR-0097, ADR-0098 |
| #045 | FX / asset-return attribution (asset performance vs. currency effect) | open | P3 | no | ADR-0102 §1 |
| #046 | Portfolio Review restructure (hierarchical tile sets) | open | P2 | no | — |
| #047 | Investor Report definition | open | P3 | no | — |
| #049 | Planning Desk: seventh area + Cash Flow Planning v1 | Shipped (2026-07-23) | P1 | yes | ADR-0104 |
| #050 | Irene: price-movement watch family (`price:*`) | open | P3 | no | — (concept at kickoff); ADR-0107 §Commissions |
| #051 | Case Workflow: the Cases area (eighth area) | shipped (2026-07-22) | P1 | no | ADR-0107 |
| #055 | Scoped Settings & Credential Architecture (application / tenant / user) | in-progress (2026-08-03) | P2 | no | ADR-0112 (concept); ADR-0095 (input) |
| #057 | Watchpoint Registry & Signal Families (Watch Desk configurability) | in-progress (2026-08-11) | P1 | no | ADR-0116; ADR-0115 (rename prerequisite) |
| #059 | Voice Configurability (per-tenant voice credentials & settings) | open | P2 | no | ADR-0118; ADR-0112 (base); ADR-0076 (input) |
| #061 | Transactions (modelling and analysis of portfolio changes) | open | P2 | no | — (concept ADR at kickoff) |

### #015 — Multi-User & Permissions

- **Formerly:** B1
- **Status:** in-progress (since 2026-05-26)
- **Priority:** P1 (prerequisite for the first release)
- **Demo-path:** no
- **ADR:** ADR-0063, ADR-0064 (ADR-0022 §4 for the per-role tool-trust overlay, B1c)
- **Dependencies:** #028 language-hygiene closure (shipped)

**Scope.** Originally planned as Phase-6 Block 2. ADR-0063 (multi-tenant activation
Phase 1) and ADR-0064 (super-admin surface) settle the role model: owner / member /
auditor plus an orthogonal platform role. Sub-items:

- **B1a' — Tenant resolver.** `services/tenant_resolution/` with `SubdomainTenantResolver`
  (audit-engine lookup on `tenants.subdomain`) and `ExplicitHostHeaderResolver` for
  tests. `done` (2026-05-26, ADR-0063 §1).
- **B1a — Schema.** Migration `b012_multi_tenant_activation`: `tenants.subdomain`,
  `tenants.is_active`, `users.roles TEXT[]` (CHECK-constrained), `users.is_super_admin`
  (CHECK bound to `SYSTEM_TENANT_ID`), Sentinel → Minathena Capital rename,
  system-tenant seed. Migration `b013_super_admin_audit` adds the platform audit table.
  `done` (2026-05-26, ADR-0063 §6, ADR-0064 §4).
- **B1b — Admin / super-admin surface.** `/super-admin/*` handlers (tenant
  list/create/deactivate/reactivate, owner password reset, super-admin user CRUD), with
  a shared `services/super_admin/operations.py` behind both the CLI subcommands and the
  routes; audit writes run in the same transaction as the underlying operation
  (ADR-0064 §4). `done` (2026-05-27, ADR-0064 §1).
- **B1d — CLI.** Bootstrap extension (system tenant, primary tenant, super-admin, owner)
  plus `create-tenant`, `create-super-admin`, `create-user`, `inspect-tenant`
  (read-only emergency path). `done` (2026-05-26, ADR-0064 §3 + §5).
- **B1e — Session-lookup refactor.** `get_optional_session` resolves the session token
  via the audit engine before a tenant context is opened; three sanctioned audit-engine
  paths are guarded by a regression test. `done` (2026-05-26, ADR-0063 §4).
- **B1c — Tool-trust per role.** Sub-classify `WRITE_INTERNAL` tools (ADR-0022 §4) so
  Shirley may only perform actions matching the session's role. `open`.
- **B1f — Tenant-owner user management.** Owner-facing surface to invite, deactivate,
  and manage roles of users within their own tenant; excluded from B1b's super-admin-only
  scope, planned when the first customer asks. Reuse point: the
  `services/super_admin/operations` layer (especially the validation helpers); a sibling
  `create_user_in_tenant` would run against the app engine in a tenant session and call
  the same validations. `open`, **P2**.

**Open design questions.** The `super_admin_audit.super_admin_user_id NOT NULL` FK
forces self-attribution on the bootstrap path (first super-admin: `actor = new_user_id`);
a follow-up migration could make the FK nullable for a cleaner "no actor" representation.

**Release-track note (2026-07-29).** #015 is not a #052 gate — the repository
flip does not depend on it. **B1c** (per-role tool-trust overlay, ADR-0022 §4)
is first-customer scope: it stays open at current priority and is
cross-referenced from **#055**, whose tenant/user credential scopes ride on the
same authorisation model.

### #017 — Shirley: Code Generation & Auto-Execution

- **Formerly:** B3
- **Status:** open
- **Priority:** P2
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** #004 Shirley base migration (shipped)

**Scope.** Extend Shirley to generate matplotlib code (and possibly other Python) that
is executed directly. Requires a sandbox and a trust model. Pipeline: Shirley produces a
Python snippet → validation → sandbox execution → result rendering. Sandbox: likely a
subprocess with restricted Python (no filesystem beyond an allowlist, no network). Trust
model: a per-user "auto-execute" vs. "always review" setting, with an audit log. Library
allowlist: matplotlib, numpy, pandas, scipy — nothing beyond.

**Open design questions.** Subprocess vs. RestrictedPython? How does the matplotlib
output return to the chat UI — inline PNG or an artifact surface?

### #018 — Shirley: Full App Control

- **Formerly:** B4
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** —
- **Dependencies:** #004 (base), #017 (execution pattern), #015 (permission boundaries)

**Scope.** Shirley should be able to operate the whole program — every function, every
area. Tool-use pattern, probably one tool definition per module: per area/module, tool
definitions in the OpenAI/Anthropic function-calling format; tool routing (Shirley picks
a tool, a FastAPI endpoint executes, the result returns to Shirley); permission
boundaries (Shirley may only do what the user may — the B1c overlay); transparent tool
calls in the chat (the tool call is visible, the result inline).

**Open design questions.** Substantial — can be decomposed into per-area sub-items
(tool definitions per area) once it becomes concrete.

### #019 — Investment-Limit Monitoring (Anlagegrenzen, Phase 7)

- **Formerly:** B5
- **Status:** in-progress (data + engine + import + read-only surface shipped 2026-05-20; editing deferred)
- **Priority:** P2
- **Demo-path:** no
- **ADR:** ADR-0055, ADR-0056, ADR-0057 (ADR-0069 non-goal points to #023 for the forward forecast)
- **Dependencies:** —

**Scope.** Investment-limit monitoring for Versorgungswerke and other regulated
institutional investors. Limits are expressed as maximum shares of AUM (e.g. "max 30%
equities"); sources are statutory (Satzung) limits and the AnlV (Anlageverordnung).
Historisation is mandatory: historical evaluations must stay reproducible when limit
sets change (statutory changes, AnlV reform).

Data layer (`done`): AnlV classification as a 1:1 investment attribute (ADR-0057,
`anlv_categories` global stammtabelle, `investments.anlv_code` nullable FK); the AUM
coverage-engine contract with cash as the residual `aum_eur − Σ NAVs` (ADR-0055, daily
series in `portfolio_aum`); limit-set historisation `(family, effective_from)` with
`family IN ('saa', 'anlv')`, immutable (ADR-0056); migration `b010`; models and
repositories.

Sub-items:

- **B5a — Engine.** `services/analytics/limit_coverage.py` (pure-functional, plan/actual
  cut-over, AUM carry-forward, limit-set selection by `effective_from`, UNALLOCATED and
  NO_LIMIT buckets). `done` (2026-05-20).
- **B5b — Excel-import path.** Limit sets and AUM series from the workbook, sum-to-100
  validation, sentinel-tenant seed. `done` (2026-05-19).
- **B5c — Read-only web surface.** `/back-office#limits` with a KPI strip, per-family
  status tables, a small-multiples coverage chart, and a limit-set history browser.
  `done` (2026-05-20).
- **B5d — Portfolio-Review integration: rejected (won't-do, operator decision 2026-07-11).**
  The Portfolio Review is a pure reporting surface for the assets and their aggregate; limit
  information does not belong on it. Limit status remains exclusively on the limits surface
  at `/back-office#limits`. This is a deliberate separation of concerns (reporting vs.
  compliance monitoring), not deferred work — the tile idea is closed, not postponed.

Follow-ups: edit mode for limit sets (CSRF, validation, audit log); an AUM-forecast warning
in the importer; PDF export of the limits surface; drill-down from class → investments.

**Open design questions.** The forward projection for the limit forecast is split out as
**#023** (ADR-0069 non-goal).

### #020 — Shirley: Analysis Reads Phase 2 (deterministic surfaces)

- **Formerly:** B6
- **Status:** open
- **Priority:** P2
- **Demo-path:** no
- **ADR:** ADR-0070 (stub)
- **Dependencies:** #024 (ADR-0069 back-office analysis tools — establishes the `READ_INTERNAL` pattern this continues)

**Scope.** Second wave of Shirley tools beyond the ADR-0069 family. Goal: Shirley sees
**every analysis deterministically reproducible from Postgres** that a human sees on the
web surface — without the user having to "run" anything first, because the tools call the
same service on demand. One thin `READ_INTERNAL` wrapper each, no new analytics:

- `get_portfolio_overview` → `FrontOfficeOverviewService.get_overview_kpis`
- `get_portfolio_review` → `PortfolioReviewService.get_portfolio_overview` /
  `get_single_investment_review` (includes the **portfolio-wide total-return index** —
  the previously hidden "index development")
- `get_benchmark_comparison` → `BenchmarkComparisonService.get_investment_comparisons`
  (Stage a) + asset-class composites (Stage b)
- `get_saa_configuration` → `SAAService.get_configuration_full` (the SAA *assumptions*)
- `get_efficient_frontier` → `PortfolioAnalysisService.compute_frontier` (**read half
  only** — view the configured frontier; re-optimising with new constraints is later /
  optimizer territory)

**Open design questions (for ADR-0070).** `get_portfolio_review` is large (the whole IC
package) — as one prose block it blows the char cap, so parametrise by `section`
(cashflows / multiples / total-return-index / treemaps) instead of a mega-dump. Which of
these reads need a chart envelope per ADR-0048 (frontier, total-return index) and which
stay prose-only? Tool count rises from ~11 to ~16; mis-selection between similar-sounding
reads (`get_investment_data` vs. `get_portfolio_review` vs. `get_portfolio_overview`)
becomes a risk — couple with #022.

**Boundary.** Pure deterministic reads. Run-bound results (scraper) belong to #021;
starting analyses (optimizer / SAA re-optimisation) is a separate, later feature.

### #021 — Persistent Analysis-Result Storage (run-bound results)

- **Formerly:** B7
- **Status:** open
- **Priority:** P2
- **Demo-path:** no
- **ADR:** ADR-0071 (stub)
- **Dependencies:** #002 Report Scraper (the first writer); ADR-0035 (RLS); ADR-0022 (trust classes)

**Scope.** Some results are **not** reproducible from a pure read, because they arise
from a user run with parameters and/or an external fetch. Trigger: the **Report Scraper**
persists nothing today — runs sit in an in-memory `OrderedDict` (`app.state.scraper_runs`),
LRU-evicted, discarded at session end (see `web/routes/scraper.py`, "persistence is
explicitly deferred"). For Shirley to "see what the user saw", the result must be stored
first. The plan: a small **generic, tenant-scoped analysis-result table** (a named,
`kind`-typed result as JSONB; schema modelled on the existing `DataStoreEntry`;
deliberately *not* per-feature). The scraper writes its results on each run; later
run-bound results (optimizer scenarios a user wants to keep) reuse the same store. Read
tools `list_analysis_results` / `get_analysis_result` (web variant) finally give the
currently **dead** in-memory `list_analysis_results` something real — and, in time, retire
the four in-memory DataStore tools.

*(This item closes a deferred persistence gap and so has a loose-end flavour; it stays
under Features because it introduces a new persistence surface and new read tools.)*

**Open design questions (for ADR-0071).** Trust provenance — scraper results are
externally fetched content (`READ_EXTERNAL_UNTRUSTED`); persistence **does not wash the
trust level away**, so on re-read Shirley must still receive them inside the
`<external_content trust="untrusted">` delimiter with the source. The store therefore
needs `trust`/`source` columns, not just the tool. Own table vs. reusing
`data_store_entry` / `data_uploads`. Retention (permanent vs. TTL) and audit-trail bearing
(BAIT/VAIT).

**Boundary.** Persistence + read. Starting new runs is not part of this item.

### #022 — Shirley: System-Prompt Grounding (dynamic tool list + dataset context)

- **Formerly:** B8
- **Status:** in-progress (tool-list half `done` 2026-06-02; dataset-context half `open`);
  remainder deferred past demo day (2026-07-11)
- **Priority:** P2
- **Demo-path:** no
- **ADR:** — (no ADR; `docs/Soul_Shirley.md` records the tool-list half as **Done (B8)**)
- **Dependencies:** ADR-0069 (more tools make it more urgent)

**Scope.** `Soul_Shirley.md` records the tool-list half as **Done (B8)**; the original ask
was that the system prompt should include a **dynamic** list of currently available tools plus
context on the loaded dataset. Today
the prompt advertises capabilities ("Strategic Asset Allocation", "risk considerations")
but does not ground Shirley on her real inventory. Each new tool wave (ADR-0069, #020)
raises mis-selection and confabulation risk. This is the load-bearing companion to the
tool expansion — should run **with** #020, not after.

- **`done` (2026-06-02, commit `e2af923`):** A dynamic prompt section from
  `ToolRegistry.get_tool_definitions()`, implemented in `AIServiceCore._render_tool_inventory`
  + `get_system_prompt` (`services/ai_service_core.py`), which build the
  `## Your currently available tools` block at prompt-assembly time from the registry.
  Test: `tests/assistants/test_system_prompt_grounding.py`.
- **`open`:** A concise dataset context (investment names, date ranges, present
  surfaces). Deliberately not built yet — `get_system_prompt` is DB-free and pulls no
  tenant context; the dataset context needs a separate, tenant-scoped load path.
- **`open`:** Clear negative hints (e.g. "no forward projection" for
  `get_limit_coverage`).

**Deferral (operator decision 2026-07-11).** The open remainder (dataset context + negative
hints) is postponed until after the demo day. The item stays `in-progress` (the tool-list
half is shipped); the coupling recommendation — run the dataset-context half together with
**#020** — is unchanged and applies at the deferred date.

### #023 — Cashflow / Exposure Projection (Forward Limit Forecast)

- **Formerly:** B9
- **Status:** **open** — the **TA slice shipped 2026-07-17** (milestone, not closure; see below).
  The **forward limit forecast remains the open remainder**, and it is the half this item was
  raised for.
- **Priority:** P2 — regraded P1 → P2 (2026-07-29): first-customer capability,
  not a release gate; the open-source publication does not need the forward
  limit forecast.
- **Demo-path:** no
- **ADR:** **ADR-0105** (the former **ADR-D**, Accepted 2026-07-14 — **commissioned by
  ADR-0104 §2**, which fixed the contract it had to satisfy; covers the **TA slice only**);
  ADR-0069 (non-goal that points here)

**Milestone — the TA slice shipped (2026-07-17, Strand 3+4).** ADR-0105 discharged the ADR-D
commission: **ephemeral** Takahashi–Alexander generation of remaining profiles for
**plan-less capital-account funds**, at the plan-world seam. The pacing rows #049 shipped
**disabled rather than hidden** are now **active** for those funds. The generation **writes
nothing** (no cashflow row, no NAV row, no source marker) and is **never calibrated to
reproduce an existing plan** (ADR-0104 D18 — where a plan exists it *is* the baseline), which
holds by construction: the generator runs only where the remaining profile is empty. Generated
flows settle against their currency's cash path through the **same `add_step` primitive** the
executors use — one settlement rule, applied at the seam rather than by the book-reading
materialisation service (closure §4.5). **v1 surfaces TA flows only:** no NAV path is generated
(E4), so **Σ NAV falls for TA funds** in the scenario lens — a deliberate posture, with the
platform-asserted NAV trajectory reserved for a successor ADR alongside the repace-NAV question
(closure §7.4).

**This item is not closed.** The **forward limit forecast** — the forward half of the
limit-forecast question, feeding #019's limit machinery with projected exposure — is untouched
by ADR-0105 and remains #023's open remainder.
- **Dependencies:** #019 limit monitoring; ADR-0069 (non-goal that points here); **#049**
  (Planning Desk — the surface that consumes the engine; ADR-D is written against its
  already-fixed overlay contract)

**Scope.** The **forward half** of the limit-forecast question ("a €40m call in Q3
against the end-2030 limits") has no engine today. ADR-0069 explicitly excludes this half,
because half a projection inside a read tool creates structural debt. A projection-capable
cashflow/exposure engine is a **prerequisite before the first release** (hence P1) and
gets its own ADR and concept discussion. Only then can a what-if overlay (an added call
against a future limit set) sensibly become a Shirley capability.

**Engine.** Forward projection of **capital calls and distributions** via a
**Takahashi–Alexander** parametric model (rate-of-contribution, rate-of-distribution,
yield, bow factor, lifetime) as the canonical private-markets cashflow forecast, with
per-strategy / per-vintage parameter sets. The model lives as a **pure analytics module**
(no DB persistence per ADR-0013; `services/analytics/` purity invariant), consuming the
existing commitment / NAV / called-capital state and emitting a projected net-cashflow and
NAV-exposure series.

**Usability layer.** An **easy-to-operate transformation surface** so the investor can
shape the expected cashflows without touching the parameters by hand: pacing assumptions
(commitment plan, deployment speed), scenario shifts (slower deployment, delayed exits,
J-curve stretch), and a side-by-side of base vs. transformed projection. The parameter
edits feed the same pure engine; the surface only assembles inputs and renders the output.

**Boundary.** A standalone engine effort. The Takahashi–Alexander module stays analytics-pure
and DB-free. **Not** to be wired "on the side" into `get_limit_coverage`. Stress/regime
scenarios beyond pacing belong to **#034** (the scenario engine consumes this projection as
its private-markets cashflow input).

**Commissions ADR-D (added 2026-07-13).** ADR-0104 §2 **commissions** this ADR without writing
it, and in doing so fixes the contract the TA engine must satisfy — so ADR-D is now written
against an existing seam rather than a green field. The engine is consumed by the `repace_flows`
transformation in two distinct roles, and the distinction is binding:

- as the **generator** for investments that have **no manager plan** — until it lands, the
  Planning Desk shows those pacing rows **disabled rather than hidden** (#049), which is the
  visible hook this item fills;
- as the **engine** behind the richer scenario regimes of **#034** (ADR-E).

It is **never calibrated to reproduce an existing manager plan** (decision **D18**): where a
plan exists, that plan *is* the baseline and `repace_flows` time-scales its remaining profile
directly (mid-position = bit-identical frames). TA generates what is missing; it does not
re-derive what the manager already told us.

**Sequencing.** Implementation is **Strand 3** of the Planning Desk programme, after **#049**.

### #032 — Regulatory Reporting Pre-Fill (BaFin / BerVersV quarterly returns)

- **Formerly:** — (new; raised 2026-06-16)
- **Status:** open
- **Priority:** P2 (substantial recurring value for the target segment; not release-blocking)
- **Demo-path:** no
- **ADR:** — (a scope/concept ADR is the first deliverable at kickoff); ADR-0074 (product scope); the AnlV-taxonomy precondition is satisfied by ADR-0083 (catalogue correction)
- **Dependencies:** corrected AnlV catalogue (ADR-0083, landed); #019 limit-coverage engine (reuse for the Mischung figures); a Sicherungsvermögen / restliches-Vermögen split and a book-value ingestion path (both currently absent — see gaps)

**Scope.** Versorgungswerke (and comparable AnlV-regulated investors) must file a set of quarterly
supervisory returns under **Anlage 2 Abschnitt C BerVersV** plus the three attachments of the
Sammelverfügung of 27.08.2021. These are filled by hand today. The six forms all hang off data the
platform already holds: **Nw 670** (composition by § 2 Abs. 1 AnlV category × asset pool),
**Nw 671** (book/fair values, hidden reserves/charges, coverage of technical provisions),
**Nw 660** (derivatives, forward purchases/sales, structured products), **Anlage Mischung**
(actual vs. permitted maxima per § 3 AnlV), **Anlage Streuung** (per-issuer limits per § 4 AnlV,
with LEI), and **Anlage Fonds** (the supervisory fund look-through / Durchschau).

The product position is **pre-fill and plausibility-check, not a reporting system of record.** The
goal is to eliminate the bulk of manual transcription and hand the filer a checked draft with an
explicit gap list — the final book-value and Sicherungsvermögen reconciliation stays with the human.
Two-thirds pre-filled is the target, not full automation.

**What the platform already holds (the spine).** AnlV classification 1:1 per investment
(`investments.anlv_code` → global `anlv_categories`, ADR-0057/0083); fair value over time
(`investment_navs.nav_value`); the AUM series (`portfolio_aum`); the AnlV limit family with
`max_pct` and the `limit_coverage` engine that already computes Ist-% per AnlV class against AUM
(#019) — that engine is effectively the arithmetic core of Anlage Mischung; and the look-through
dimensions (`investment_rating_weight` for the Bonität breakdown, `investment_region_weight` +
`region_country_membership` for the EWR/non-EWR splits, plus sector/maturity weights).

**The real gaps (honestly ranked).**
1. **Book value (Buchwert) is absent** — only fair value (NAV) is modelled. Nw 671 and Anlage Fonds
   need Buchwert *and* Zeitwert *and* hidden reserves/charges (= Zeitwert − Buchwert). Book value is
   an HGB / Bestandsführung figure that belongs to the investment accounting, deliberately **not**
   to a portfolio-analytics tool. Cleanest boundary: do not become the system of record here.
2. **No Sicherungsvermögen / restliches-Vermögen split** — all three asset-pool columns depend on
   it. Structurally small (a per-investment, or per-lot, SV-membership flag) but mandatory.
3. **Issuer + LEI absent for Streuung** — only `manager_name` exists. Streuung exempts funds
   (Nr. 15–17), so for a FoF-heavy book this is a partial gap on the direct bond/loan book plus
   single-issuer look-through.
4. **No derivatives model (Nw 660)** — for a FoF/PE/Infra/Private-Debt book this form is frequently a
   Fehlanzeige or limited to currency hedges; lowest leverage, defer or omit.
5. **Fund look-through (Anlage Fonds) only partial** — the per-investment weights are *a*
   look-through but not the AnlV-category-level Durchschau with Bonität/ABS/Hedgefonds/transparency
   that the form demands. In practice that data is delivered by the KVG / Master-KVG / Depotbank as a
   standardised file (BVI data carrier, VAG/Tripartite reporting); a structured-file ingest problem,
   a possible Report-Scraper hook, not a calculation problem.

**Sub-items (proposed; sequenced by leverage per effort).**
- **#032a — AnlV-taxonomy correction.** Reconcile the `anlv_categories` catalogue with the actual
  § 2 Abs. 1 AnlV numbering. **Landed via ADR-0083** (precondition satisfied); was independently
  overdue because #019's Mischung evaluation keys off this catalogue.
- **#032b — Anlage Mischung pre-fill.** Highest quick-win: almost fully derivable from the existing
  coverage engine (Ist-Bestand per AnlV class, Ist-%, Höchstsatz). Needs the SV denominator and the
  § 3 AnlV row mapping.
- **#032c — Nw 670 scaffold.** Category rows + fair values + Bonität/EWR breakdowns; Buchwert columns
  and the SV split remain flagged as gaps.
- **#032d — Anlage Streuung.** Once issuer/LEI are modelled.
- **#032e — Nw 671 / Anlage Fonds / Nw 660.** Later/optional; depend on book value, the Durchschau
  file, and a derivatives model respectively.

**Output format.** The forms are positional, machine-read PDFs. For the MVP, emit an **xlsx mirror
per form** (1:1 row/column structure, with a gap list and a provenance column per value) rather than
pixel-placing into the official PDFs. A PDF overlay (reportlab) can follow later, analogous to the
planned kaleido→ReportLab path (#001).

**Open design questions.** Where does the SV-membership flag live — on `investments`, or on a
finer-grained lot/holding entity that does not yet exist? **Cross-reference (2026-07-13, linkage
only — the resolution stays this item's concern):** the finer-grained entity the question reaches
for now exists. The **`position_transactions` ledger** (#038, ADR-0097) is the natural home of
lot/holding attribution, and therefore of the SV-membership question — a per-lot flag has
somewhere to live that it did not have when this item was raised. This is a pointer, not a
decision: whether SV membership is a per-investment or a per-lot property is still open, and
still #032's to settle. How is book value ingested
(custodian/Bestandsführung file vs. an Excel column) without turning the tool into an accounting
system? Mischung and Streuung must be reported on a **look-through** basis ("direkt und indirekt
gehalten"): the direct layer (a fund as its Nr. 16/17 line) is trivial, but the indirect look-through
to the underlying § 2 categories needs the KVG Durchschau data — the quick value is the direct layer
plus plausibility-checking, the true regulatory figure needs the Durchschau feed.

---

### #033 — Watch Desk (proactive heartbeat / Irene)

- **Formerly:** — (new; raised 2026-06-28)
- **Status:** in-progress (first slice landed 2026-07-02, migration `b019`)
- **Priority:** P2
- **Demo-path:** no
- **ADR:** ADR-0085 (Irene Persistence Layer), ADR-0086 (Irene Cadence and Tick Adapter),
  ADR-0087 (Irene Delta Mechanics), ADR-0088 (Irene Synthesis Contract), ADR-0089 (Decision
  Console — Briefing UI and Action Model) — the concept/scope ADR became this accepted stack
- **Dependencies:** #019 limit monitoring, #020/#024 read tools (the data-access seam it
  reuses), #023 cashflow projection (forward deltas), #034 scenario analysis (stress deltas)

**Scope.** A dedicated Watch Desk segment driven by a **heartbeat**: a proactive AI agent
(**Irene**, the proactive counterpart to the reactive **Shirley**) that periodically checks
whether there is **action required** (`Handlungsbedarf`) and, when there is, surfaces a card
with a concrete proposed course of action. This is the proactive-intelligence layer on top of
the existing reactive assistant — the user does not have to ask; the Watch Desk watches
and raises findings.

**Architecture (concept).** A **read-only data-access layer** that reuses the existing
read-tool seam (#020/#024) plus external signals (RSS); a heartbeat / **delta-detection** loop
against a **decision journal** (functional memory of what was already raised and resolved —
not an audit artifact); **edge-triggered** card surfacing via a `surface_finding` tool
(structured output) so a standing condition is raised once, not on every beat; and an
**urgency 1–10** scale mapped to behavioural bands (informational / notable / critical) that
govern how prominently a finding is shown and whether it pushes (e.g. Telegram).

**The hard part.** The difficulty is **materiality calibration over time**, not the technical
plumbing — deciding which deltas are worth surfacing and avoiding alert fatigue. The journal +
edge-trigger design exists to make that calibration tractable; expect the threshold/band logic
to be tuned iteratively against real findings.

**Boundary.** Read-only and advisory. The Watch Desk proposes; it does not execute portfolio
actions. Write-capable actions remain governed by the #015/#018 permission and tool-trust
model.

**Delivered so far (2026-07-02, migration `b019`; ADRs 0085–0089).** The first slice is
implemented and tested: the two-table append-only persistence layer — findings plus decision
journal (ADR-0085); the delta engine and the edge-triggered beat (ADR-0086, ADR-0087); the
**non-streaming** `surface_finding` synthesis contract (ADR-0088); the out-of-process CLI tick
adapter `cli/irene_tick.py` with its systemd service/timer units (ADR-0086); and the Watch
Desk web surface — the sixth Area (ADR-0089) with its Briefing / Journal / Watchlist
(since DC4/D5: **Calibration**) / Scenarios (removed by #049, ADR-0104 §8)
sections (`modules/watch_desk/`), routes (`web/routes/watch_desk.py`),
and templates. Still **open**: the materiality calibration over time (the hard part above) and
the fuller proactive-agent behaviour — which is why this item is `in-progress`, not `done`.

**The Scenarios section leaves this Area (2026-07-13, ADR-0104 §8 — amends ADR-0089).** Of the
four sections above, **Scenarios** was never more than a placeholder anchor for **#034**.
ADR-0104 re-anchors #034 to the **Planning Desk** (the seventh Area, **#049**), and #049
**deletes** `modules/watch_desk/scenarios.py` and its partial rather than migrating them.
The Watch Desk therefore ends up with **three** sections — Briefing / Journal / Calibration
(renamed from Watchlist by the One-Glass refresh, DC4/D5) — losing a dead panel and gaining a
crisper identity: it *watches and raises*, the Planning Desk *projects and simulates*. Nothing
else about #033 changes.

### #034 — Scenario Analysis (economic downturn / rate / stress)

- **Formerly:** — (new; raised 2026-06-28)
- **Status:** **v1-scope shipped (2026-07-17)** — the ADR-0104 Scenario Analysis slice is live
  as the Planning Desk's `scenario_analysis` section (Strand 3+4, closure
  `docs/handover/strand-3plus4-adr-0104-0105-closure.md`). **ADR-E stays commissioned-open**
  for the wider vision; the item is **not closed** and does not move to **Shipped**.
- **Priority:** P2
- **Demo-path:** no
- **ADR:** **ADR-0104** (§2 overlay contract, §5 results, §6/§8 the section it lives in — the
  scope/concept ADR this item was to write at kickoff is **substantially pre-empted**);
  **ADR-E** (own ADR, **pending — commissioned-open**: scenario timing regimes beyond
  immediate-t₀, and the wider vision below)

**Shipped in v1 (2026-07-17, Strand 3+4).** The `market_shock` and `fx_shock` surfaces against
the four-kind overlay contract — no fifth kind added; `fx_shock` acts at the **conversion seam**
rather than as a registered executor (closure §3.1, "Shape A"). Results are assembled
**deltas-first over the existing engines** (ADR-0104 §5) and rendered as a baseline/scenario
chart pair on shared axes with a ghost baseline, KPI deltas, and limit headroom. `market_shock`
v1 is **level-shift, immediate-t₀ only** (E6), strictly after the seam. **Demo-proven**: the
live demo on the S34.5 state passed.

**Open remainder (ADR-E).** The richer scenario regimes: **timing** beyond immediate-t₀ (ramp,
lag, decay), **rate/duration** (parallel and non-parallel shifts through the ADR-0079/0081/0082
fixed-income analytics), **spreads**, and **default/recovery**. Scenario **persistence** (named
parameter sets) and **Shirley/Irene access** to the surface also remain out of scope
(closure §7.2/§7.3).
- **Dependencies:** **#049** (the Planning Desk area and the overlay contract this item extends);
  #023 cashflow projection (private-markets cashflow input + the pure-engine pattern); #019 limit
  monitoring (forward limit breaches under a scenario); the liquid archetypes (ADR-0079/0081/0082
  — rating/maturity/duration analytics for rate shocks)

**Scope.** A genuine scenario / stress engine spanning the simulable dimensions of the book:
**economic-downturn scenarios** (equity drawdown, NAV write-downs, distribution slowdown,
J-curve stretch on the private side), **interest-rate scenarios** (parallel and non-parallel
shifts, applied through the Fixed-Income duration/OAS analytics), and the other materially
simulable factors (spread widening, FX, default/recovery on the credit book, commitment-pacing
regimes). Each scenario produces a portfolio-level impact view — NAV, exposures, and forward
limit headroom under the stressed path.

**Engine.** A **pure analytics module** (DB-free, FastAPI-free, Qt-free — analytics-layer
purity invariant), consuming the same state the projection engine uses, parameterised by a
**scenario regime** (a named set of shocks). It composes with **#023**: the Takahashi–Alexander
projection supplies the private-markets cashflow path, the scenario layer applies the shocks,
and the result can feed both the **Watch Desk** (#033, stress deltas) and the forward
limit forecast (#019/#023).

**Open design questions.** Deterministic shock scenarios first vs. a Monte-Carlo distribution
later; where scenario definitions live (a fixture catalogue of named regimes vs. user-defined);
how look-through interacts with shocks (shock the fund line vs. the underlying § 2 categories).
*(The last of the original four — how scenario results are surfaced — is now answered; see the
re-anchor below.)*

**Re-anchored to the Planning Desk (2026-07-13, ADR-0104 §6/§8).** This item **no longer lives
in the Watch Desk**. It becomes the `scenario-analysis` **section of the Planning Desk**
(the seventh Area, #049), and the ADR-0089 placeholder anchor it was going to fill —
`modules/watch_desk/scenarios.py` and its partial — is **deleted by #049**, not migrated
into. The surfacing question is thereby settled: scenario results are shown on the Planning Desk,
alongside the baseline projection they are deltas against, rather than on the Watch Desk or as
an overlay on the review/limit surfaces.

**What ADR-0104 already fixed for this item.** Three things it no longer has to decide:

- the **overlay contract** (§2) — a scenario is a **parameter set, not a dataset**; this item
  ships the `market_shock` and `fx_shock` **surfaces** against the four-kind contract #049
  establishes, and adds no fifth kind;
- **`fx_shock` is a first-class kind**, not a scope variant of `market_shock` (decision **N3**):
  it acts on the **conversion seam** rather than on values, and hiding that behind a scope
  discriminant would bury a concealed branch inside one executor;
- **results are deltas-first** (§5), computed over the **existing** engines on transformed
  frames — no engine forks, no parallel scenario engine.

**Commissions ADR-E (added 2026-07-13).** What ADR-0104 deliberately did *not* decide, and this
item owns: **scenario timing regimes beyond immediate-t₀**. The v1 `market_shock` is a price- or
NAV-level shift applied at t₀; paths, lagged shocks, and mean-reverting regimes are ADR-E
territory. **#023** (ADR-D) feeds the richer regimes with its Takahashi–Alexander projection.

**Sequencing.** Implementation is **Strand 4** of the Planning Desk programme, after **#049**.

### #036 — Live Data Import (provider-agnostic market-data ingest)

- **Formerly:** — (new; raised 2026-07-03)
- **Status:** in-progress (slices 1–5 + ADR-0096 surface + Bloomberg adapter landed 2026-07-06/07; live smoke + deferred items open)
- **Priority:** P2
- **Demo-path:** no
- **ADR:** ADR-0090 (Investment Security Identifiers and FIGI Normalisation), ADR-0091
  (Market-Data Provider Port, Normalised DTO, and Adapter Architecture), ADR-0092 (Live-Ingest
  Contract and Excel Precedence), ADR-0093 (Live-Import Trigger and Out-of-Process Tick Adapter),
  ADR-0096 (Identifier Scheme-Set Extension — Provider-Native Fund Identifiers)
- **Dependencies:** the Excel import format (ADR-0009) as the precedence baseline (ADR-0092);
  the investment domain (security-identifier columns, ADR-0090)

**Scope.** A provider-agnostic pipeline that ingests live market data — prices/NAVs and the
associated security reference data — **alongside** the existing Excel import rather than
replacing it. The design is set out across ADRs 0090–0093, and most of it has shipped:
slices 1–5 (identifier table `b020`, Excel identifier ingestion, the `services/market_data/`
provider architecture, the live-ingest contract + Excel precedence `b021`, and the trigger /
out-of-process tick `b022`), the ADR-0096 scheme-set extension and identifier-CRUD surface
(`b023`), the Stage-1 `CredentialResolver` seam (ADR-0095 §1), and the fixture-validated
Bloomberg Desktop-API adapter are all in the tree. What remains open is the live smoke against
an entitled Bloomberg Terminal and the **Deferred items** listed below — the item stays
`in-progress` until those close.

**Architecture (per the ADR set).** Security instruments are keyed by normalised identifiers
with FIGI as the canonical anchor (ADR-0090); market data enters through a **provider port**
with a normalised DTO and pluggable adapters, so no provider SDK leaks past the seam
(ADR-0091); the **live-ingest contract** defines how ingested values reconcile with the Excel
baseline, with Excel taking precedence where the two overlap (ADR-0092); and ingestion runs
through an **out-of-process tick adapter** on a trigger, mirroring the Irene tick pattern
(ADR-0093).

**Status note.** ADRs 0090–0093 were accepted on 2026-07-06. Implementation slice 1 — the
`investment_identifiers` table (migration `b020`), its ORM model, and repository per ADR-0090
— has landed. Slice 2 — Excel identifier ingestion — has landed too: the extractor parses the
optional `ISIN` / `Ticker` Attributes rows and the transform service reconciles the
`source='excel'` identifier subset per investment (excel book-of-record semantics) with
ISIN-else-ticker primary promotion. Slice 3 — the market-data architecture (ADR-0091) —
has landed too: the `services/market_data/` package with an async `MarketDataProvider`
port, the provider-blind `NormalizedSeries` DTO, a declarative capability matrix
(`config/market_data_capabilities.yaml`), Yahoo (native-async), synthetic (fixture-driven
test-event), and OpenFIGI ISIN/ticker→FIGI adapters. Slice 4 — the live-ingest contract
(ADR-0092, `ingest_origin` + Excel-precedence conditional upsert, migration `b021`,
`InvestmentService.ingest_normalized_series`) — has landed. Slice 5 — the trigger and
out-of-process tick (ADR-0093) — has landed: migration `b022` (`market_data_schedule`), a
per-tenant market-data **system actor** and disabled **schedule** seeded through
`seed_tenant_defaults` / bootstrap, the market-linked predicate helper
(`services/investments/market_linked.py`), the per-tenant refresh core
(`services/investments/live_refresh.py`), the `portfoliflow market-data-tick` CLI with a
`market_data`-domain advisory lock, systemd units, and the Admin "Refresh now" web surface.
The **identifier scheme-set extension and CRUD surface** (ADR-0096) has landed: migration
`b023` swaps the `investment_identifiers.scheme` CHECK to add the provider-native
`preqin` / `pitchbook` schemes (the two code frozensets updated in the same commit and
pinned by `tests/regression/test_identifier_scheme_set_consistency.py`), and the
investment detail page gains a **Security Identifiers** panel — view / add / delete /
set-primary — backed by three nested-resource routes and thin `InvestmentService`
methods (`add_identifier_manual`, `set_primary_identifier`, `delete_identifier`,
`list_identifiers`). Provider-native IDs enter only through this **human-confirmed**
surface with `source='manual'`; no import path auto-writes a fuzzy provider-ID mapping
(ADR-0096 §2). The **Bloomberg Desktop-API adapter** (ADR-0091) has landed
**fixture-validated**: `services/market_data/adapters/bloomberg.py` (async, a single
`asyncio.to_thread` bridge over a synchronous gateway seam that lazily imports `blpapi`;
`figi`/`isin` security topics; `nav_price` from daily `PX_LAST` with currency from the
`CRNCY` reference field) plus a `bloomberg` capability-matrix entry that ships
`enabled: false`. The live smoke against a real, entitled Terminal remains the gated
activation step. FIGI persistence into `investment_identifiers` and a weight-DTO
successor ADR remain to come.

**Deferred items (tracked).**

- **ADR-0096 — Identifier scheme-set extension (provider-native fund IDs). Implemented
  2026-07-07 (migration `b023`).** The closed `investment_identifiers.scheme` set now
  carries `preqin` / `pitchbook`, and the **identifier CRUD web surface** (view / add /
  delete / set-primary) is live on the investment detail page. Remaining deferred items
  under ADR-0096's outlook:
    - the **live-eligibility predicate's generalisation** from "listed + primary market
      identifier" to "capability-reachable" — deferred with a named trigger: the first
      private-markets adapter going live (ADR-0096 §3); until then `market_linked.py`
      stays as implemented;
    - **fuzzy / LLM-assisted mapping proposals** (name/vintage/manager similarity) are
      explicitly future work — this slice ships only the human-operated surface
      (ADR-0096 §2).
- **Bloomberg Desktop-API adapter — landed fixture-validated 2026-07-07 (ADR-0091).**
  The adapter is implemented and tested against a fake gateway (no `blpapi`, no
  network); the `bloomberg` matrix entry ships `enabled: false`. **The live smoke
  against a real, entitled Bloomberg Terminal remains the gated step**
  (entitlement via the known channels). Operator activation checklist: install
  `blpapi` from Bloomberg's own pip index on the Terminal machine, set
  `BLPAPI_HOST`/`BLPAPI_PORT` if not on the defaults, flip `enabled: true`, run a
  `--tenant`-scoped manual tick as the live smoke, then rely on the timer path.
  The **credentialed** Bloomberg variants (Server API / B-PIPE / Data License)
  stay #037-gated — separate future adapters, not a mode of the Desktop-API one.
- **Remaining #036 deferred items (restated).** **FIGI persistence** into
  `investment_identifiers` (writing resolved `figi` rows with `source='openfigi'`), the
  **weight-DTO successor ADR** (the `NormalizedSeries` bucket dimension the five
  `weight_*` kinds need), the **live-eligibility predicate generalisation** (trigger:
  the first private-markets adapter going live, ADR-0096 §3), and the **Preqin /
  PitchBook adapters** (gated on API access) remain open later slices.
- **Provider credential management (Stage 2) — #037.** Stage 1 (the environment-only
  `CredentialResolver` seam, ADR-0095 §1/§3) landed alongside slice 5; the per-tenant
  credential vault (table + Fernet encryption + re-encrypt CLI + tenant-admin surface) is
  tracked as its own feature, **#037**.

---

### #037 — Provider Credential Management (per-tenant market-data credentials)

- **Formerly:** — (new; raised 2026-07-07)
- **Status:** won't-do (2026-07-29) — superseded by #055 (Stage 1 shipped
  2026-07-07 and remains in service)
- **Priority:** P1 (demo-relevant enabler — the first institutional live-data question is
  "how do *our* credentials get in?")
- **Demo-path:** no
- **ADR:** ADR-0095 (Provider Credential Vault — Per-Tenant Market-Data Credentials with
  Staged Adoption)
- **Dependencies:** **#015** (Multi-User & Permissions) for the tenant-admin authorisation
  model — Stage 2 either lists #015 as a hard dependency or ships a minimal owner-role gate
  as its vanguard (ADR-0095 §4); **#036** (Live Data Import) for the provider set and the
  refresh core the resolver plugs into

**Scope.** Per-`(tenant, provider)` market-data credentials, so tenant-licensed data
(Bloomberg / Preqin / PitchBook) is served from **the tenant's own** entitlement rather
than an operator-global key — a licensing/compliance requirement (BAIT/VAIT attribution)
that env-only configuration cannot express. ADR-0095 fixes the design and stages the
adoption.

**Stage 1 (delivered 2026-07-07).** The `CredentialResolver` seam
(`services/investments/credential_resolver.py`, parallel to `live_refresh.py`, **never**
inside the DB-free `services/market_data/` layer) resolves an opaque `ProviderCredential`
per provider and hands plain values to the adapters, keeping them credential-source-blind.
Stage 1 implements the **environment** source only, with the resolution order of ADR-0095
§1 encoded explicitly (a source list Stage 2's vault source prepends to). Each provider's
credential policy — `env_fallback: allowed | forbidden`, `optional` — is a static,
non-loosenable declaration in `config/market_data_capabilities.yaml` (ADR-0095 §2:
`yahoo` / `synthetic` = `none`, `openfigi` = env-fallback-allowed + optional). The OpenFIGI
API-key read moved out of `services/market_data/normalisation.py` (now injected as a
parameter). No schema, no migration.

**Stage 2 (open).** The tenant vault: a `provider_credentials` table (tenant-scoped, RLS
via `apply_tenant_rls`; `provider`, encrypted JSONB `payload_ciphertext`, `enabled`,
reserved-nullable `user_id`, audit columns), **application-level Fernet encryption** of the
serialised payload with the master key from the environment (`CREDENTIAL_VAULT_MASTER_KEY`,
never stored in the DB), key rotation as a documented operator procedure via a **re-encrypt
CLI** command (decrypt-old → encrypt-new in one transaction), and a **tenant-admin
management surface** with write-only credential fields (stored values never rendered back;
display shows provider / enabled / set-unset and at most a last-4 hint), payloads never in
logs or audit rows. The tenant-admin area does not exist yet and is created as part of this
stage; its authorisation model depends on **#015** (ADR-0095 §4). No KMS/HSM, no per-user
credentials (column reserved, semantics deferred), no cross-tenant session proxying
(categorically out, ADR-0095 §2/§5).

**Superseded (2026-07-29, → #055).** The three-scope settings model
(application / tenant / user) absorbs this item's direction and widens it beyond
market data to LLM and Telegram credentials. ADR-0095's Stage-2 spec becomes
input to the #055 concept ADR, not a parallel item. Nothing shipped is undone:
Stage 1 (the `CredentialResolver` seam) stays in service.

---

### #038 — Position Model (Transactions, Holdings, and Unitised Valuation)

- **Formerly:** — (new; raised 2026-07-08)
- **Status:** in-progress (strands S0–S5 landed and tested, 2026-07-08/09; the operator
  browser walkthrough is pending, and synthetic unitisation of private-markets positions
  remains deferred per ADR-0097 §8 with a named trigger)
- **Priority:** P1
- **Demo-path:** no
- **ADR:** ADR-0097 (Position Model — Transaction Ledger, Holdings Derivation,
  Valuation Modes, and Instrument Prices), ADR-0098 (Computed-NAV Materialisation and
  Live-Ingest Write-Path Re-Routing)
- **Dependencies:** #036 (Live Data Import — the write-path re-routing modifies its
  ingest seam); the Excel import format (ADR-0009 lineage, test data v30)

**Resolved 2026-07-09: S0 shipped the interim guard; S3 landed the structural
re-routing. Kept for the record.**

**⚠️ P0 guard note (finding F1, verified 2026-07-08).** The live ingest path writes
**per-share prices into position-level NAV series**: `SeriesKind.NAV_PRICE` (Yahoo
unadjusted close / Bloomberg `PX_LAST`, magnitude 10²) routes into
`investment_navs.nav_value`, where the Excel book of record stores position values
(magnitude 10⁷–10⁸ in the test data). The ADR-0092 guard prevents overwrites but
**inserts** on dates where the book is silent, producing NAV series that jump between
millions and hundreds. Sibling finding **F6**: Yahoo `dividend` events are per-share
amounts, ingested unscaled into position-level `investment_cashflows`. **Live series
ingest for listed instruments is unsafe until strand S0 (interim guard) ships, and must
not be activated for any listed instrument until strand S3 (re-routing) lands.**

**Scope.** A transaction-driven, unitised position model for listed instruments: a
`position_transactions` ledger, an `instrument_prices` per-unit price series, pure
holdings derivation, `investments.valuation_mode` (`'reported'` | `'unitised'`), a
synchronous computed-NAV materialisation service (`basis='computed'`,
`ingest_origin='system'`), and the per-mode re-routing of live `nav_price` / per-share
flow ingest. Consumers of the NAV-series contract (analytics, charts, limits, Irene,
reporting) remain unchanged — the write side is the entire blast radius. Synthetic
unitisation of private-markets positions is **specified in ADR-0097 §8 and deferred**
with a named trigger. Excel import format gains an optional `Units` row (v30).

**Blocks** (must land before):

- Planning Desk build-out (successor of "Simulation & Planning"; #034 scenario work
  included)
- Scenario Analysis engine (#034)
- BerVersV pre-fill implementation (#032)
- **Activation of live market-data ingest for any listed instrument** (see P0 guard
  note)
- Any test campaign that would legitimately flag the latent unit mismatch as a defect

**Blocked-by note resolved (2026-07-13).** The forward references in that list were
placeholders for successors that did not exist as items yet. They do now, and the note
resolves to them: the **Planning Desk build-out** is **#049** (ADR-0104), and the **cash**
successor it depends on is **#048** (ADR-0103). #034 and #032 keep their own IDs. Nothing about
#038's own state changes — it is the **dependency**, and strands S0–S5 have landed; the
successors simply have names now.

**Interacts with:** #036 (write-path re-routing of the ingest seam; `market_linked.py`
predicate extension). The two ADRs this item pinned constraints for **have been written and
accepted**, and both honour those constraints:

- the former **ADR-A** is **ADR-0103** (cash as first-class asset class, **#048**) — the pinned
  constraint holds: cash is expressible as `valuation_mode='unitised'` with `price ≡ 1.0000`,
  which is exactly the representation ADR-0103 adopts;
- the former **ADR-C** is **ADR-0104** (plan-path / scenario overlay, **#049**) — the pinned
  constraint holds: plan rows stay **value-based**, and hypothetical transactions **never** write
  `position_transactions`, `investment_navs`, or `instrument_prices` (ADR-0104 §2, annex §B.2).

**Strand table (frozen 2026-07-08):**

| Strand | Scope | Depends on | Migration | Landed |
|---|---|---|---|---|
| **S0** | Interim ingest guard: refuse per-share series kinds (`nav_price`, `dividend`, `coupon`) at ingest; defect fix closing F1/F6, no schema, no semantics change | none — ships immediately, before ADR acceptance | none | landed 2026-07-08 — **retired by S3** |
| **S1** | Schema per ADR-0097: `position_transactions`, `instrument_prices`, `investments.valuation_mode`, RLS, repositories, holdings derivation (pure), unprivileged-role tests | ADR-0097 accepted | `b024` | landed 2026-07-08 |
| **S2** | Materialisation service per ADR-0098 §1–3: `ingest_origin` `'system'` extension, classify-then-write, stranded-row deletion, regression tests | S1; ADR-0098 accepted | `b025` | landed 2026-07-08 |
| **S3** | Ingest re-routing per ADR-0098 §4: per-mode `nav_price` routing, dividend/coupon holdings-scaling, `market_linked` predicate extension, **S0 guard retired** | S1, S2 | none | landed 2026-07-08 |
| **S4** | Import format v30: optional `Units` / `Units As Of` Attributes rows → `opening` transaction synthesis; test data v30 | S1 | none | landed 2026-07-09 |
| **S5** | Web surfaces: positions panel on investment detail, transaction entry, mode-flip action | S1–S3 | none | landed 2026-07-09 |

S1+S2 may be merged into a single Fable session at the operator's discretion (they form
one coherent responsibility block); the frozen default is separate sessions, preserving
one-concern-per-prompt discipline. S4 may run parallel to S2/S3 once S1 has landed.

### #045 — FX / asset-return attribution (asset performance vs. currency effect)

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** ADR-0102 §1 (raises it, and declines the local-currency view in its favour);
  ADR-0102 §Alternatives Option C (the dual-track measurement this supersedes);
  ADR-0099 §6 (the local-vs-functional measurement question in its original form)
- **Dependencies:** ADR-0102 (landed) — the functional-currency measurement basis this
  decomposes

**The gap.** Since ADR-0102 every return the statistics / SAA / benchmark layer reports is
measured in the tenant's **functional currency, FX effect included** — what a
functional-currency investor actually experiences, and consistent with the review path's
IRR/TVPI/DPI. That is the right headline number, but it is a *composite*: a USD fund's
functional-currency return blends the manager's asset performance with the EUR/USD move, and
the surface cannot today tell the user which is which. A 12% functional return on a fund that
returned 4% in USD while the dollar appreciated 8% reads identically to one that returned 12%
on flat FX — an attribution question the platform's institutional users will ask the moment
they see a mixed-currency book.

ADR-0102 §1 declined to answer it by the obvious shortcut. A raw **local-currency**
volatility or return, offered as a parallel secondary view, is exactly half of an attribution
model, and shipping half is a confusing partial: returns and levels would disagree on
currency basis inside one surface (Alternatives, "Local-currency returns only"), and the
dual-track hedged-vs-unhedged view (Option C) is the finance-complete answer only once the
decomposition sits behind it. Hence: functional-only now, attribution later. **The
local-currency perspective lives here** — this item is where it is built, not a gap in
ADR-0102.

**Resolution.** Decompose the functional-currency return into an **asset-performance**
component (the position's return in its own currency) and a **currency** component (the FX
move over the same window), with a residual/interaction term handled explicitly rather than
silently absorbed. The inputs already exist and need no new supply: `services/fx/` holds the
point-in-time, carry-forward rate frame, and the three services already assemble both the raw
position-currency series and the converted one at the same seam — the decomposition is a pure
calculation over the two, so it belongs in `services/analytics/` (DB-free, ADR-0013) with the
assembly staying in the services above the ADR-0099 §4 boundary. Two things need deciding
before code: the **linking convention** for multi-period attribution (Carino / Menchero
smoothing versus a simple arithmetic split — the classic multi-period residual problem, and
the substantive methodological choice this item owns), and the **surface** the result is
disclosed on (a statistics-section breakdown, a benchmark-attribution column, or both).
Expect a successor ADR carrying those two decisions; the seam work itself is small.

### #046 — Portfolio Review restructure (hierarchical tile sets)

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** open
- **Priority:** P2
- **Demo-path:** no
- **ADR:** — (design decision at kickoff; expected to be a short surface ADR)
- **Dependencies:** #013 (Portfolio-Overview section, shipped); direct successor of **#010**
  (won't-do)

**Scope.** Restructure the Portfolio Review from a single aggregate view into a
hierarchical, **show-everything** report structure:

1. the **portfolio-level aggregate** tile set (exactly as today);
2. an **aggregate tile set per sub-asset class** (e.g. Private Equity, Listed Equities);
3. a **6-tile set per investment**.

There is **no filter and no selection interaction** — the page shows the full hierarchy in a
fixed order. This produces a long page ("many slides") **by design**: the restructured review
is the template and starting basis for the Investor Report (**#047**). The rejected
alternative — letting the reader narrow the report to a chosen subset — is #010, closed as
won't-do on the same date.

**Notes.** Aggregation per sub-asset class and per investment should reuse the **existing
converted review seam** (ADR-0099 §4 / ADR-0102): the service already accepts
`investment_ids`, which the per-class and per-investment tile sets can use **server-side** —
the parameter #010 leaves in place. Single-currency invisibility and `MissingFxRateError`
semantics apply here exactly as everywhere else on the converted seams.

### #047 — Investor Report definition

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** — (own concept ADR at kickoff; potentially large)
- **Dependencies:** **#046** (its template / starting basis); related: **#001** (Portfolio
  Review PDF export — expected to converge with this item's output path when both mature)

**Scope.** Define the **Investor Report** as a product artefact: its content and structure
(building on the #046 hierarchy — portfolio aggregate → sub-asset-class aggregates →
per-investment tile sets), its **periodicity**, its **audience**, the **narrative /
commentary blocks**, **branding**, and the **output format**. Definition first (a concept
ADR), implementation after. This is the perspective #010 was implicitly reaching for; making
it an item of its own is what allowed #010 to be closed rather than reinterpreted.

**Scheduling note (operator decision 2026-07-11).** Explicitly **low priority**; scheduled
well after the demo day.

### #049 — Planning Desk: seventh area + Cash Flow Planning v1

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** **Shipped (2026-07-23)** — unblocked 2026-07-13 when **#048** shipped; built out
  over Strands 2–4 (ADR-0104/0105) and flipped to Shipped once the Strand-2 §10 operator gates
  were filled (full suite `3444 passed`/exit 0 at head b031, plan-data gate re-verified, S2.6
  confirmed from code, demo smoke attested — see `strand-2-adr-0104-closure.md` §10)
- **Priority:** P1
- **Demo-path:** **yes**
- **ADR:** **ADR-0104** (Accepted 2026-07-13)
- **Dependencies:** **#048**; interaction basis
  `docs/handover/planning-desk-interaction-decisions.md` +
  `docs/handover/planning-desk-mockup-v2.html` (both 2026-07-13, fixed before the ADR text per
  the mock-before-implement rule)
- **Blocks:** **#034** (the Scenario Analysis section lives here)

**The gap.** No forward-looking **work** surface exists. The book carries everything a planner
needs — actual and plan NAV series, plan flows, the materialised cash plan path (#048), and one
FX conversion seam — but there is nowhere for a human to *work* with the future: stretch a
drawdown, insert a hypothetical trade, apply a shock. The Watch Desk's `scenarios.py` is
an ADR-0089 placeholder anchor, not a surface.

**Scope.** #049 delivers the **`planning_desk` area** — the **seventh** Area, an architectural
addition ADR-0104 is the required ADR for — with its registry entry, two stacked sections, and
a sticky parameter strip; the **Cash Flow Planning** lens (per-currency balance timeline over
the ADR-0060 seam, plus pacing rows); **hypothetical-transaction entry** into the overlay
parameter set; and the **baseline projection** over the plan world.

**The overlay contract (ADR-0104 §2)** is the load-bearing design: a scenario is a **parameter
set, not a dataset**. An overlay is an ordered list of transformations applied to assembled
baseline frames, of exactly four kinds — `insert_transaction`, `repace_flows`, `market_shock`,
`fx_shock` — each a **pure** `frames → frames` executor in a DB-free overlay module the ADR-0013
purity guard extends to. Nothing an overlay does ever writes `position_transactions`,
`investment_navs`, or `instrument_prices`; engines consume transformed frames **unchanged**, so
no engine forks. This item ships the first two kinds' surfaces; **`market_shock` and `fx_shock`
surfaces ship with #034**, against the same contract.

**Pacing rows ship disabled where no manager plan exists.** `repace_flows` time-scales the
*remaining* manager-plan drawdown profile; a fund without a manager plan has nothing to scale
until the Takahashi–Alexander engine (**#023**, ADR-D) generates one. The row is shown
**disabled rather than hidden** — the honest empty state, and the visible hook for #023.

**Retires** `modules/watch_desk/scenarios.py` and its partial (ADR-0104 §8) — the
ADR-0089 placeholder anchor is deleted, not migrated, and the Watch Desk loses a dead
panel.

### #050 — Irene: price-movement watch family (`price:*`)

- **Formerly:** — (new item; no legacy A/B/C/D ID)
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** — (own concept at kickoff); commissioned by **ADR-0107 §Commissions**
- **Origin:** commissioned in ADR-0107 §Commissions (Case Workflow concept chat, July 2026).

**Scope.** A new watch-subject family **`price:*`** for Irene, so that tenant-definable
thresholds on **instrument-price movements** (e.g. a strong rise or a drawdown over a
configurable window) surface as findings through the **existing** machinery — subject keys,
bands, edge detection, re-trigger deltas — with **no changes to that machinery's design**.
Thresholds are defined by the operator / tenant, **not hardcoded**; the **Calibration**
section shows them read-only, exactly like the existing families.

**Explicitly out of scope.** Any **Case-Workflow coupling** (cases consume findings
source-agnostically per ADR-0107) and any **new analytics**.

**Why it exists.** Market-movement events currently surface only *indirectly*, via RSS press
clusters; a definable price threshold makes them **first-class, deterministic** findings.

### #051 — Case Workflow: the Cases area (eighth area)

- **Formerly:** — (new item; no legacy A/B/C/D ID). Successor of the **"Execution Network"**
  concept kernel, which was deliberately cut down to the case workspace alone.
- **Status:** shipped (2026-07-22)
- **Priority:** P1
- **Demo-path:** no
- **ADR:** **ADR-0107** (Accepted 2026-07-20, ahead of implementation)
- **Dependencies:** #033 (Irene findings — the origin a case references); the Watch Desk
  "One Glass" refresh (DC1–DC5, closed 2026-07-20), whose DC3 ships the **disabled** "Open
  case →" affordance this item arms
- **Binding mock:** `docs/handover/cases-area-mockup-v2.html`

**The gap.** The Watch Desk observes and reports but does not help decide, and nothing
connects a surfaced finding to what the manager subsequently did about it.

**Scope.** A **Case** is an open question about the portfolio, worked to a documented close.
The item delivers **`cases`** as the **eighth Area** — placed **between Watch Desk and
Planning Desk** in `_AREAS` and the sidebar (ADR-0107 Decision 1), route `/cases` — with three
surfaces: **Open cases** (the self-filling to-do list, "Mine" as a filter and never a data
boundary), **Case detail** (origin, append-only timeline, status/linked-objects rail), and
**Recently closed** (last 5, plus a collapsed archive search over titles and closing notes
only). Persistence is two tenant-scoped RLS tables, `case` and `case_entry`, the latter
**append-only**. One member — `opened_case` — joins the finding-resolution vocabulary; since
that vocabulary is application-enforced rather than a SQL enum, it is a code + test change.

**Adding an eighth Area is the architecturally significant part** — the precedents are
Watch Desk (sixth, ADR-0089) and Planning Desk (seventh, ADR-0104 §6). CLAUDE.md,
`docs/architecture.md`, and the ADR-0084 glossary all said **seven** before this item; it
updated them, not the reverse.

**Explicitly out of scope.** The **provider directory** and any **engagement protocol** from
the original Execution Network brainstorm — cut entirely, and leaving **no visible trace in the
shipped UI** (no ghost buttons, no "coming soon"). A case **cannot link to a scenario**:
Planning-Desk scenarios are stateless (ADR-0104), so a case freezes a **snapshot** of the
already-rendered result view model instead (ADR-0107 Decision 6) — nothing new is computed.
Attachment contents stay outside search (the DMS boundary, Decision 7).

**Release context.** ADR-0107 records this as the **last feature set before the AGPL release**
is prepared (followed by a dedicated UI-polish pass): the shipped surface must be complete in
itself, with nothing disabled or promised.

**Shipped (2026-07-22).** Strands **C0–C6 complete** per `docs/handover/case-workflow-strand-closure-note.md`
(implementation window 2026-07-20 → 2026-07-22, migration **`b031`**): the eighth Area is live, the
Watch Desk **arms** the "Open case →" affordance (`opened_case`) and **merges** closed cases into
the Journal, the Planning Desk **pins** scenario snapshots to a case, and Shirley carries a per-session
**case brief** plus a **consultation-excerpt pin**. The **seven→eight documentation reconciliation**
(CLAUDE.md, `docs/architecture.md`, `readme.md`) landed in the same change; gate evidence is recorded in
the closure note **§6**, and the strand's ID-less follow-ups remain in the closure note's registers.
Moved to the **Shipped** record.

### #055 — Scoped Settings & Credential Architecture

- **Formerly:** — (new; raised 2026-07-29; absorbs the direction of #037)
- **Status:** in-progress (2026-08-03)
- **Priority:** P2 (first-customer capability, not release-blocking)
- **Demo-path:** no
- **ADR:** ADR-0112 (concept, Accepted 2026-08-03); ADR-0095 as input (§1–§3
  remain authoritative; §4 superseded by ADR-0112 §2)
- **Dependencies:** **#015** (Multi-User & Permissions) for the tenant/user-scope
  authorisation model (cross-reference: B1c tool-trust overlay); #036 for the
  provider set the credential half plugs into

**Scope.** A three-scope settings model — **application / tenant / user** — as
one architecture for provider credentials **including LLM (OpenRouter) and
Telegram**, not only market data. Resolution order, storage, encryption,
rotation, and the management surface are fixed by a concept ADR first; ADR-0095's
Stage-2 spec (Fernet vault keyed by `CREDENTIAL_VAULT_MASTER_KEY`, re-encrypt
rotation CLI, write-only/masked tenant-admin surface) enters that ADR as input
rather than living on as a parallel item (#037 → superseded). Stage 1 (the
`CredentialResolver` seam, shipped 2026-07-07) remains in service unchanged and
is the natural application-scope floor of the model.

### #057 — Watchpoint Registry & Signal Families (Watch Desk configurability)

- **Formerly:** — (new; raised 2026-08-10). **Delivers #050** (`price:*` watch family),
  which was commissioned by ADR-0107 §Commissions and is subsumed here rather than run
  separately — the price family could not land without the registry underneath it.
- **Status:** in-progress (2026-08-11)
- **Priority:** P1 (release-blocking: it precedes the **#052** AGPL flip)
- **Demo-path:** no
- **ADR:** **ADR-0116** (Watchpoint Registry and Signal Families); **ADR-0115**
  (Watch Desk rename — implemented first, this programme uses the new names throughout)
- **Dependencies:** #033 (the Watch Desk itself — findings, bands, edge detection,
  re-trigger deltas); ADR-0103 §6 (the materialised cash plan path the `liquidity`
  family reads); the position model's `instrument_prices` (#038) and the `fx_rates`
  dataset (#042 supplies it live; Excel supplies it today)

**The gap.** What the Watch Desk observed, and at which thresholds, was configured
nowhere in the UI. Subjects were derived (limit sets, the closed `_KNOWN_TAGS`
vocabulary), thresholds were code constants in `FloorConfig`, and the only editable
element was the cadence. Recalibration meant editing code and redeploying,
application-wide. Worse for the product: autonomous observation of *quota utilisation
alone* undersold the platform's central capability, over data it already held.

**Scope delivered (P1–P6, two commits each).** ADR-0115's rename first (P1), then:
**P2** — migration `b033`: the historised, RLS-protected, audit-triggered `watchpoints`
and `floor_calibration` tables, their models and repositories, and the idempotent
default-watchpoint seeder (`portfoliflow seed-watchpoints` for the post-import re-run).
**P3** — the single per-tenant resolution both the beat and the monitor consume, the
per-subject sensitivity overlays for the derived families, and the Calibration section
grown from a three-cell fact display into the tenant calibration editor (with the four
pinned invariants rendered locked). **P4** — the `price` and `fx` producers and their
beat evaluation, plus the scoping of the un-mutable-breach rule to the quota families:
a watchpoint the operator set themselves is theirs to silence. **P5** — the `freshness`
and `liquidity` producers. **P6** — the four families rendered on the monitor with live
derivation, the add / adjust / retire / history flows, the Calibration watchpoint list,
and this documentation pass.

**Magnitude-semantics deviation record (binding, no successor ADR).** Two families
depart from the ADR-0116 §4 magnitude table, and identically. The ADR states
`freshness` as "days the newest NAV **exceeds** `max_age_days`" and `liquidity` as
"shortfall in coverage-ratio pp **below** `min_coverage_ratio`". Both are zero right up
until the threshold is crossed, so there is no value strictly between calm and
triggered for the WARN band to occupy — the families would silently degrade to a binary,
contradicting the same section's rule that WARN means "within the warn fraction of the
trigger threshold". As implemented, `freshness` measures the **age itself** against
`max_age_days` as the threshold, and `liquidity` measures **percent of the way down to
the floor** (`100 × min_coverage_ratio ÷ ratio`) against a fixed 100-point threshold —
the identical machinery `price` and `fx` already run on, and the reason Approaching
exists for them at all. `liquidity`'s 100-scale is arithmetic, not communication: every
human-facing string speaks in ratios (`1.08× against your 1.20× floor`) and the
100-scale never renders — asserted against the rendered templates. Approved with the P5
implementation prompt (2026-08-11); recorded **here** rather than by amending ADR-0116,
which is filed and stays as written, and **no successor ADR** is opened: the deviation
refines two cells of one table without touching a decision. The rationale is restated in
the two producers' module docstrings, where the next reader of the code will be.

**Commissions — open candidates (IDs to be issued).** All four are *named* here and
designed nowhere; none is half-built in the shipped surface.

1. **Class-level `price` selectors** — one watchpoint spanning an asset class and
   auto-covering new instruments. Needs its own concept work on dynamic subject identity
   and history: a subject that appears mid-life has no versioned row saying when it
   started being watched.
2. **Book-derived `fx` pair sets** — "all pairs in the book", auto-following. The same
   dynamic-subject concern, plus a seeding-vs-following ambiguity the v1 seeder sidesteps
   by deriving pairs **once**, at seed time.
3. **`price` direction configurability** — up / both. v1 watches declines only (the
   long-book assumption), deliberately as a fixed rule rather than a hidden option.
4. **`pacing:*` family** — strictly after **#023** / the TA engine. Plan-deviation
   watching has no reliable reference object until that exists, and a family measured
   against a projection nobody trusts would teach an operator to ignore the desk.

**Explicitly out of scope (binding).** No limit-set CRUD — the #019 remainder stays
deferred and untouched, and the quota groups keep "manage in Limits" rather than growing
a second edit point for a ceiling. No per-user watchpoints (tenant scope only), no
auto-mute, no Irene-initiated watchpoint changes of any kind. No editing of the pinned
invariants. No RSS tag-vocabulary editing.

### #059 — Voice Configurability (per-tenant voice credentials & settings)

- **Formerly:** — (new; raised 2026-08-12)
- **Status:** open
- **Priority:** P2
- **Demo-path:** no
- **ADR:** ADR-0118 (annex amendment to ADR-0112 §3, Accepted 2026-08-12); ADR-0076 (voice service, input)
- **Dependencies:** #055 apparatus (taxonomy, `CredentialResolver`, vault, Admin → Providers & Credentials — all shipped); scheduling free relative to #052 (see note)

**The gap.** Voice STT/TTS credentials are application-scope only (`.env`,
ADR-0076; explicitly pinned out of taxonomy v1 by ADR-0112 §3). In a
multi-tenant deployment every tenant's voice usage therefore runs on the
operator's central API key at the operator's cost, and neither the voice
service's enablement nor Shirley's persona voice can differ per tenant. This
is the same economic-and-isolation logic that moved OpenRouter and Telegram
into `scoped_settings` (#055) — not a convenience feature.

**Resolution.** Per ADR-0118: three taxonomy declarations — `voice`
(config-only: `enabled`), `voice_stt` (`api_key` secret; `model`, `base_url`
config) and `voice_tts` (`api_key` secret; `model`, `voice` config) — all
tenant-scope, `env_fallback=True`, `optional=False`, preserving ADR-0076's
STT/TTS provider-mixing freedom under ADR-0112 §1's unit-chaining rule. A
`ResolvedVoice` value object (masked repr, mirroring `ResolvedLLM`) carries
one turn's resolution; both surfaces (web `/chat/voice` + `/chat/tts`,
Telegram voice handler) resolve per request inside the tenant context, and
`voice_enabled` becomes a per-tenant answer computed per render/turn. The
module-level singletons in `services/voice/` are retired; the application
scope is served by the session-less resolver's env source. UI is
taxonomy-driven appearance on Providers & Credentials only (labels,
descriptions, hints, "live" pills) — no bespoke settings page. Strand
V1–V6 (taxonomy → value object → web surface → Telegram surface →
singleton retirement → UI polish & docs), estimated 4–5 sessions.

**Scheduling note.** The strand may land before or after the AGPL public
release (#052) without architectural consequence (ADR-0118). V1–V2 are
purely additive; V3–V5 touch live surfaces.

---

### #060 — Watch Desk area-level deactivation

- **Formerly:** — (new; raised 2026-08-13)
- **Status:** open
- **Priority:** P3
- **Demo-path:** no
- **ADR:** — (own ADR required; commissioned by ADR-0119)
- **Dependencies:** none blocking; ADR-0119's enabled-by-default seed is what
  makes the switch worth having, not a prerequisite for building it

**The gap.** A tenant can silence the Watch Desk's *beat* — `irene_schedule`
carries an `enabled` flag the cadence panel writes — but it cannot turn the
**Area** off. The sidebar entry, the sections, the routes and the Irene
affordances are all unconditional. A tenant with no interest in automated
monitoring therefore carries a permanently visible Area it does not use, and
since ADR-0119 §4 the schedule row now seeds *enabled*, which sharpens the
question rather than creating it: disabling the beat leaves the surface
behind, and there is no single answer to "is this tenant a Watch Desk
tenant?".

**Resolution (sketch, not a decision).** A complete on/off switch for the
Area: one tenant-level flag with well-defined consequences across the
sidebar, the routes, the beat, the Journal, and the Cases arming path — plus
a stated position on what happens to findings and cases that already exist
when the Area is switched off (hidden, frozen, or read-only), which is the
part that actually needs deciding. **A separate ADR is required**: the
question spans the ADR-0058 sidebar/IA contract, the ADR-0086 tick, and
ADR-0107's arming seam, so it is a topology decision, not a settings toggle.
Previously assessed at roughly **one new ADR plus three implementation
prompts**.

---

### #061 — Transactions (modelling and analysis of portfolio changes)

- **Formerly:** — (new; raised 2026-08-16)
- **Status:** open — future functionality; scope not yet fixed
- **Priority:** P2
- **Demo-path:** no
- **ADR:** — (concept ADR required at kickoff)
- **Dependencies:** #038 (Position Model — the `position_transactions` ledger
  is the write-side substrate this would analyse); interacts with #034 / #049
  (Planning Desk hypothetical transactions) and #023 (forward projection)

**The gap.** The platform models portfolio *state* — NAVs, cashflows, holdings
— and answers "what does the portfolio look like". It has no first-class notion
of a portfolio *change*: a buy, sell, switch, or rebalance as an object that can
be modelled before it happens and analysed after it has. The two existing
transaction-shaped constructs do not cover it: #038's `position_transactions`
ledger is a write-path and valuation construct for unitised instruments (it
feeds computed NAVs, ADR-0097/0098), and the Planning Desk's hypothetical
transactions are scenario-local overlays that by contract never write the book
(ADR-0104 §2). Neither gives the user a way to state an intended change, see its
effect on allocation, limits, liquidity and return, and later compare it against
what actually happened.

**Resolution (open).** To be fixed by a concept ADR at kickoff. The questions
that ADR must settle, at minimum: whether a transaction is booked, proposed, or
both, and how the two relate; the boundary against #038's ledger (reuse the
table, or a distinct planning-level object above it); the boundary against the
ADR-0104 overlay contract, which must keep holding — no overlay writes to the
book; which Area the surface belongs to (Back Office as the booking home, or
Planning Desk as the modelling home, or a split); and what "analyse a change"
resolves to concretely — attribution of the delta, pre/post limit and liquidity
checks, turnover, or realised-vs-intended comparison. Until that ADR exists this
item is a placeholder for the capability, not a specification of it.

---

## Shipped (record)

A passive archive of completed items. This is not a backlog category; it keeps the two
active lists lean. Implementation detail for these items lives in their ADRs,
`docs/architecture.md`, and git history.

| ID | Title | Shipped on | ADR |
|---|---|---|---|
| #002 | Report Scraper (Assistants) | 2026-05-19 | ADR-0053 |
| #004 | Shirley base migration (Assistants) | 2026-05-15 | ADR-0048–0052 |
| #005 | SAA Back Office (Strategic Asset Allocation) | 2026-05-17 | ADR-0054 |
| #006 | Asset-class name in the charts article header | 2026-05-12 | — (Phase-6 closure) |
| #008 | Single-investment Portfolio-Review surface in sidebar IA | 2026-06-01 | ADR-0073 |
| #009 | Statistics latency investigation (N+1 fix) | 2026-05 | ADR-0065 |
| #011 | Region model for country aggregation | 2026-05-13 | ADR-0046 |
| #012 | Benchmarks & Attribution (Phase 1) | 2026-05-25 | ADR-0061, ADR-0062 |
| #013 | Front-Office "Overview" KPI strip | 2026-05-29 | ADR-0067, ADR-0068 |
| #014 | Front-Office "Overview" chart row + fund-composition Pareto | 2026-06-03 | ADR-0072 |
| #016 | GUI Migration & Qt Sunset (Stage 1 — PyQt6 surface removed) | 2026-07-02 | ADR-0094 |
| #024 | Shirley: Back-Office analysis tools (Phase 1) | 2026-06-01 | ADR-0069 |
| #026 | Commit DB-reset / init scripts | 2026-05-12 | — |
| #027 | Document the `db/init/` mechanism | 2026-07-11 | — |
| #028 | Block-0 language & privacy hygiene closure | 2026-05-10 | — |
| #029 | Multimodal image input for Shirley (web + Telegram) | 2026-06-04 | ADR-0075 |
| #030 | Voice I/O for Shirley — STT + TTS (web + Telegram) | 2026-06-04 | ADR-0076 |
| #039 | Migration-roundtrip test design | 2026-07-10 | — |
| #040 | Statistics / SAA currency contract | 2026-07-11 | ADR-0102 |
| #043 | Glossary v3: currency terms | 2026-07-11 | — |
| #048 | Cash as first-class asset class (ADR-0103 implementation) | 2026-07-13 | ADR-0103 |
| #049 | Planning Desk: seventh area + Cash Flow Planning v1 | 2026-07-23 | ADR-0104 |
| #051 | Case Workflow: the Cases area (eighth Area) | 2026-07-22 | ADR-0107 |
| #052 | AGPL Public Release Track | 2026-08-16 | — |
| #053 | UI Polish Pass (all eight Areas) | 2026-08-16 | — |
| #054 | CI & Lint/Typecheck Hardening | 2026-08-16 | ADR-0109, ADR-0110 |

---

## ID crosswalk

Maps every old per-category ID to its new flat ID. Inbound references in the ADRs
(`roadmap A2`, `roadmap B9`, …) resolve through this table; the ADRs are not rewritten.

| Old ID | New ID | Bucket |
|---|---|---|
| A1 | #001 | Loose ends |
| A2 | #002 | Shipped |
| A3 | #003 | Loose ends |
| A4 | #004 | Shipped |
| A5 | #005 | Shipped |
| A6a | #006 | Shipped |
| A6b | #007 | Loose ends |
| A7 | #008 | Shipped |
| A8 | #009 | Shipped |
| A9 | #010 | Loose ends |
| A10 | #011 | Shipped |
| A12 | #012 | Shipped |
| A13 | #013 | Shipped |
| A14 | #014 | Shipped |
| B1 | #015 | Features |
| B2 | #016 | Shipped |
| B3 | #017 | Features |
| B4 | #018 | Features |
| B5 | #019 | Features |
| B6 | #020 | Features |
| B7 | #021 | Features |
| B8 | #022 | Features |
| B9 | #023 | Features |
| B10 | #024 | Shipped |
| C1 | #025 | Loose ends |
| D1 | #026 | Shipped |
| D2 | #027 | Loose ends |
| D3 | #028 | Shipped |

There was never an `A11` in the source numbering — the gap is preserved, no item was
lost. Sub-item labels (B1a–B1f, B2a/B2b, B5a–B5d) live inside their parent item and are
not renumbered.

---

## Change log

| Date | Change |
|---|---|
| 2026-08-21 | **ADR-0124 complete (I1–I4) — installation and release distribution.** Engine-neutral, parameterisable container bootstrap; guided installer `scripts/install.sh` (remote and local mode, six phases, `--doctor`, bash-3.2 / BSD-userland contract, documented exit-code table, never `sudo`); the `stable` branch advanced by `promote-stable.yml` on calver tags (deploy-key bypass, version guard); `release-assets.yml` publishing the GitHub Release entry and the `install.sh.sha256` asset; `installer.yml` CI (shellcheck, a Linux end-to-end install including the exit-13 idempotence and secret-free-transcript assertions, and the macOS bash-3.2 contract; weekly schedule); README installation rewritten as Options A/B/C. Deviations are recorded in the respective commits: D1–D3 (I2), D1–D2 (I3), D1–D2 (I4). This is a **local** install path only — it does not touch **#025 Hetzner Deployment**, which stays open. No roadmap item carried the work; next free ID unchanged. |
| 2026-08-16 | **AGPL public release — #052 shipped; #053 and #054 closed with it.** All eight #052 gates ticked; repository flipped to public with a fresh history. Pre-release cleanup in the same cut: primary-tenant identity unified on Minathena Capital across tests and ADRs (text substitution only, no ADR decision changed), `SECURITY.md` added (ADR-0108 D-list extended), project contact e-mail added to CONTRIBUTING / TRADEMARKS / README / CLAs, Claude Code settings excluded from the repository. Next free ID unchanged. |
| 2026-08-16 | **#061 Transactions raised (Features, P2, open, concept ADR at kickoff); next free ID `#061` → `#062`.** Operator-raised placeholder for a future capability: modelling and analysing *changes* to the portfolio — buy / sell / switch / rebalance as a first-class object with a before-and-after — as distinct from the portfolio *state* the platform models today. Filed under **Features** because it is a capability that does not exist, not an unfinished state. It is deliberately **not** covered by the two existing transaction-shaped constructs: **#038**'s `position_transactions` ledger is a write-path and valuation construct for unitised instruments (ADR-0097/0098), and the Planning Desk's hypothetical transactions are scenario-local overlays that never write the book (ADR-0104 §2). Scope is **not fixed**: a concept ADR at kickoff must settle booked-vs-proposed, the boundary against #038's ledger and the ADR-0104 overlay contract, the Area the surface belongs to, and what "analyse a change" concretely resolves to. |
| 2026-08-13 | **#060 Watch Desk area-level deactivation raised (Features, P3, open, own ADR required); next free ID `#060` → `#061`.** Commissioned by **ADR-0119** (Watch Desk cadence vocabulary v1, anchor semantics, Irene schedule seeding) in its §Consequences, and filed under **Features** rather than Loose ends because it is a capability the platform does not have, not an unfinished state: `irene_schedule.enabled` silences the *beat*, but the **Area** — sidebar entry, sections, routes, Irene affordances — is unconditional, so a tenant uninterested in automated monitoring carries a surface it never uses. ADR-0119 §4's decision to seed the schedule row *enabled* sharpens the question without creating it. **A separate ADR is required**: the switch spans the ADR-0058 sidebar/IA contract, the ADR-0086 tick and ADR-0107's arming seam, and the genuinely open part is what happens to findings and cases that already exist when the Area is switched off (hidden, frozen, or read-only) — a topology decision, not a settings toggle. Previously assessed at roughly **one ADR plus three implementation prompts**. |
| 2026-08-11 | **#058 Built-in Tick Scheduler raised (Loose ends, P1, open, ADR-0117); ADR-0115, ADR-0116 and ADR-0117 accepted.** ADR-0117 (built-in tick scheduler — in-process default with external opt-out) fills the **tick-source seam** ADR-0086 drew and ADR-0093 reused 1:1: an asyncio tick task in the web lifespan becomes the **default** tick source, the per-tick orchestration moves into one engine-parametrised runner (`services/scheduler/tick_runner.py`) that the CLI ticks become thin wrappers over, the cross-tenant due reads join the audit-engine sanctioned-path set as **path 5** (read-only, the two schedule tables only), configuration is **deployment-scope** (`TICK_SCHEDULER_ENABLED`, `TICK_SCHEDULER_INTERVAL_SECONDS` in the environment — never database, never UI), and the UI gains the scheduler's *health* rather than its settings; the systemd units under `docs/deploy/` are demoted from "the deployment" to a documented opt-out, themselves untouched. Per-tenant cadence is unchanged — this ADR changes only *what ticks*, never *who is due when* — and internal/external coexistence is safe by construction (`pg_try_advisory_xact_lock` deduplicates). No schema and no migration; next free roadmap ID `#058` → `#059`. Two **in-file corrections** landed with acceptance: the header's roadmap reference `#057` → **`#058`** (the id had already been absorbed by the ADR-0116 programme when the ADR was filed), which retires the numbering notes carried here and in `docs/adr/README.md` rather than perpetuating them under the `#044` precedent; and a **§3 precision fix** against the delivered runner — it receives **one** engine and carries the per-tenant transactions on it as well, so the superuser-privileged surface is the cross-tenant due reads alone, because `tenant_context` switches each tenant transaction to the unprivileged `APP_DB_ROLE` (ADR-0078) and RLS is enforced regardless of the connecting role; this supersedes the earlier "runs on the app engine / the audit engine never executes a beat" wording. **ADR-0115** (Watch Desk rename) and **ADR-0116** (watchpoint registry and signal families) flip `Proposed` → **Accepted (2026-08-11)** in the same pass — Revision-History row plus mirrored index status, no other content change. Documentation-only; the #058 implementation (S2–S4) is tracked in its section. |
| 2026-08-11 | **#057 Watchpoint Registry & Signal Families raised (Features, P1, in-progress); next free ID `#057` → `#058`.** The ADR-0116 programme's roadmap home, filed under **Features** because it is a genuine product extension — the Watch Desk stops being configured in code — rather than migration close-out. It **delivers #050** (`price:*`), which was commissioned by ADR-0107 §Commissions and could not have landed without the registry underneath it; #050's status flip is an operator call, left open here. **Scope:** migration **`b033`** (the historised, audit-triggered `watchpoints` and `floor_calibration` tables plus the idempotent seeder), the single per-tenant resolution the beat and the monitor share, the Calibration section grown into the tenant calibration editor with four pinned invariants rendered locked, four pure signal producers (`price`, `fx`, `freshness`, `liquidity`) on one badness-unit contract, and the P6 surface — the four families rendered on the monitor with **live derivation** (same resolution, same batched fetch, same producers as the beat; `tests/regression/test_watch_desk_single_resolution.py` pins all three layers), the add / adjust / retire / history flows, and the Calibration watchpoint list. The entry carries the **magnitude-semantics deviation record** for `freshness` (age-based) and `liquidity` (100-scale, ratio-facing), approved with the P5 prompt on 2026-08-11: ADR-0116 is deliberately **not** amended and **no successor ADR** is opened. Four commissions are named as open candidates without IDs — class-level `price` selectors, book-derived `fx` pair sets, `price` direction configurability, and a `pacing:*` family strictly after **#023**. Documentation-side, `docs/architecture.md` and `readme.md` were reconciled to head **`b033`** in the same pass. The programme precedes the **#052** release flip. |
| 2026-08-07 | **ADR-0113 acceptance recorded; documentation reconciliation pass.** ADR-0113 (Front-Office Charts — unified axis end, plan-tail display, hero de-clipping; **Accepted 2026-08-05**) carries no roadmap ID of its own: it was raised inside the **#053** UI-polish strand (Chat E2) and is tracked there; its operator gates (browser walkthrough, full suite, two parked follow-up tickets) are recorded in the strand notes. This same pass reconciled the steering documents to head **`b032`** and the **`minathena-capital`** demo identity (`CLAUDE.md`, `docs/architecture.md`, `readme.md`, the operator handbook, the deploy READMEs, and comment-level code references) — documentation-only, no status or gate changed. |
| 2026-08-05 | **ADR-0114 accepted; #056 Chart Snapshot Persistence raised (Loose ends, P1, in-progress).** ADR-0114 (Chart Snapshot Persistence — Session Rehydration and Case Pinning) flipped `Proposed` → **Accepted** with a Revision-History row (the ADR-0102/0113 precedent); `docs/adr/README.md` gained the index row and the next free ADR number moved **0114 → 0115**. **#056** is filed under **Loose ends** — it finishes what **ADR-0050 explicitly deferred** as the artefact-rehydration strand rather than adding a capability. **Scope: two seams, one artefact format.** (1) **Chat** — a per-session chart-artefact **sidecar** held beside the ADR-0050 history and never inside it (the Plotly spec must not re-enter the LLM's token stream), captured in the SSE `chart_artifact` branch for `chart_format == "plotly"` only, sharing the history's session-LRU, its "new chat"/logout lifecycle and its turn-group trim (an evicted group takes its artefacts with it — no orphaned specs); `GET /chat/history` interleaves the artefacts at their message positions and a client-side initialiser feeds each stored spec to `Plotly.newPlot` through the same render path the live SSE case uses. (2) **Cases** — **`chart_snapshot`**, the fourth pin artefact class after `document`, `scenario_snapshot` and `consultation`, in the C6 dialog/gate idiom (comment → artefact resolvable → case exists → case open, closed-case immutability unchanged): the dialog and POST carry the sidecar **`artifact_id` only** — transport by reference, the client never serialises the spec — and the server embeds the resolved spec in the journal payload so the **case record is self-contained**, rendered verbatim with nothing recomputed (the C5 snapshot discipline). **The snapshot principle is the decision:** no replay, no recomputation, no silent refresh on any surface — a case is a decision record, and reopening it must show what the decision-maker saw. **Cap:** one guard at the single capture point, `_CHART_SPEC_BYTE_CAP = 1 MiB` — an oversized spec is not archived and rehydration renders a calm placeholder, but the **live render is never refused** (degrade, never refuse). **No new table and no migration anywhere:** the chat seam stays in-memory under ADR-0050's contract (migration trigger 2 unchanged — when it fires the sidecar migrates with the messages), the case seam reuses the existing journal-entry JSONB payload. Out of scope by decision: replay/recompute of any kind (ADR-0114 §Alternatives), PNG-artefact archival, the Telegram surface, and any change to `services/ai_service_core.py`, the chart tools, the tool registry or the SSE event vocabulary. Next free ID → **#057**. The flip to `shipped` waits on the operator gate (full suite + browser walk). |
| 2026-08-03 | **ADR-0112 accepted — #055 concept fixed; item → in-progress.** Chat F (kickoff 2026-08-03) delivered the #055 concept ADR against a fresh Repomix snapshot (head `b031`). **ADR-0112** (Scoped Settings & Credential Architecture — Application / Tenant / User) is **Accepted**: three-scope model with per-setting resolution chains (default `user → tenant → application(env)`; `env_fallback: forbidden` semantics preserved per provider, ADR-0095 §2), **one** `scoped_settings` table (tenant RLS, `UNIQUE NULLS NOT DISTINCT`, per-field key-value rows, Fernet for `is_secret` rows only, master key `CREDENTIAL_VAULT_MASTER_KEY` never in DB, `vault-generate-key`/`vault-rotate-key` CLI on the superuser engine), provider taxonomy v1 (openrouter, telegram, openfigi live-chained; voice pinned application; credentialed market-data adapters schema-ready only — v1 has none live, recorded honestly), the `CredentialResolver` as single façade with the vault source **prepended** to Stage 1's ordered list, and — the structural decision — **per-turn/beat LLM client resolution** replacing the process-global configure-once posture of the tenant-blind `AIServiceCore` singleton (Irene tick generalises "no key → no-op" to per-tenant skip-with-log). Telegram target design fixed (one aiogram process, dispatcher per tenant token, pairing-code user↔chat binding replacing `TELEGRAM_ALLOWED_USER_IDS`), implementation deferred to strand F5. Admin surface "Providers & Credentials": tenant scope via `require_role("owner")` (ADR-0052 precedent — the kickoff's assumed `application_settings` guard does not exist; the section is a placeholder), user scope via `require_session` self-service, application scope stays `.env` in v1; #015 B1c boundary stated explicitly. **Supersessions:** ADR-0095 §4 (storage) → ADR-0112 §2 (`provider_credentials` never created; Revision-History row appended to 0095); ADR-0052 persistence banner retired by strands F3/F4. **Verified findings recorded in the ADR:** the Stage-1 resolver has zero productive call sites (the substantive migration is LLM/Telegram, not market data); `cryptography` (Fernet) was missing from `pyproject.toml` and enters with F1; `WEB_HOST`/`WEB_PORT`/`SESSION_COOKIE_SECURE` were undocumented in `.env.example` and enter with F0 (`SESSION_COOKIE_SECURE=true` mandatory behind TLS on the hosted instance). Annex A (full settings inventory with pinned/chained classification) and Annex B (strand table **F0…F5**, each one CC prompt + restricted tests + operator gate; F5 owns its gate and possibly a successor ADR) travel inside the ADR. `docs/adr/README.md`: index row 0112, next free ADR number → **0113**. Migration numbers claimed at implementation time. Next free roadmap ID unchanged → **#056**. Documentation-only; no code shipped in this pass. |
| 2026-07-29 | **Release track raised (#052–#055); #023 and #037 regraded.** Four new items in one cut. **#052 AGPL Public Release Track** (Loose ends, P1) — the umbrella carrying the ordered eight-gate release checklist: logo → EUIPO similarity search → trademark filing (word marks PortfoliFLOW + Happy Computer Collective, Nizza 9/42/36, **before or with publication**) → licensing apparatus (LICENSE/CLA/CONTRIBUTING/TRADEMARKS/README; **CLA before any external contribution — non-reversible**) → CI green on `main` (#054) → flake-fix closure → UI-polish closure (#053) → repository flip to public; gates are ticked by their closing chats. **#053 UI Polish Pass** (Loose ends, P1, blocks #052 gate 7) — one systematic pass over all eight Areas (findings register → implementation → gate review); functional findings become new items; **#046 stays out**. **#054 CI & Lint/Typecheck Hardening** (Loose ends, P1, blocks #052 gate 5) — ruff rule set + format, GitHub Actions with tiered test execution, typecheck islands; detail in its own chat; **seam to #025**: CI here, deployment/CD stays in #025 (#025's Resolution wording adjusted accordingly). **#055 Scoped Settings & Credential Architecture** (Features, P2) — three-scope model (application/tenant/user) for provider credentials incl. LLM and Telegram, concept ADR first; **absorbs the direction of #037**, whose ADR-0095 Stage-2 spec becomes input to the #055 ADR. **Regrades:** **#023** P1 → P2 (first-customer — the open-source publication does not need the forward limit forecast); **#025** stays P1 but is explicitly **not** a #052 gate (repo flip vs. hosted instance are parallel tracks; only the AGPL-§13 source link, Chat B, touches the release); **#037** → `won't-do (2026-07-29) — superseded by #055` (Stage 1 stays in service); **#015** confirmed first-customer for B1c, cross-referenced from #055, not a #052 gate, priority unchanged. **Housekeeping:** the #033 "Delivered so far" sentence de-stranded with history-preserving parentheticals (Watchlist → Calibration per DC4/D5; Scenarios removed per ADR-0104 §8) — the open-ends-inventory 2026-07-22 roadmap residual is discharged; the #049 shipped entry needed no action (already recorded 2026-07-23 with gate evidence). Next free ID → **#056**. Documentation-only; no code, no ADR bodies touched. |
| 2026-07-23 | **#049 Planning Desk flipped to Shipped** — the Strand-2 §10 operator gates in `docs/handover/strand-2-adr-0104-closure.md` were filled, so the blocker the Strand 3+4 closure §8 flagged is discharged and **#049** moves `open (#048 shipped)` → **`Shipped (2026-07-23)`** (summary row, detail block, and Shipped record). **Gate evidence:** full suite **`3444 passed, 3 skipped, 7 deselected (integration), 1 xfailed`, exit 0** (1:46:26) at head **b031** — the §7.3 benchmarks timeout did not recur (`669e370` + the `tests/web` autouse bot-disable); the **plan-data gate re-verified** by a clean `reset-dev` + full **v32** import (23 investments/0 errors, `cash_plan` `inserted=64`, 21 non-cash × 1706 plan NAVs, `investor_flow` only on the two cash positions, cash-plan points Cash EUR 47 + Cash USD 17, duplicate-cash check none); **§6 rounding table** regenerated from `repaced_date`; **§3.17 / §4.1 / §10.1 (S2.6)** confirmed from code (buy/sell-only; currency `readonly` → hypothetical-vs-actual divergence nil; landed `8e7692b`); **§10.4 demo smoke** operator-attested. The Strand-3+4 closure §8 dependency block was updated in step. Next free ID unchanged → **#052**. |
| 2026-07-22 | **#051 Case Workflow shipped** — the **Cases** area (ADR-0107) set `shipped (2026-07-22)` (summary row + detail block) and added to the **Shipped** record. Strands **C0–C6 complete** per `docs/handover/case-workflow-strand-closure-note.md` (implementation window 2026-07-20 → 2026-07-22, migration **`b031`** adds the three tenant-scoped RLS tables `cases` / `case_entries` / `case_attachments`): the **eighth Area** is live between Decision Console and Planning Desk in `_AREAS` and the sidebar (route `/cases`; three surfaces — Open cases / Case detail / Recently closed + archive); the Decision Console **arms** the previously-disabled "Open case →" affordance (the fifth finding-resolution `opened_case`) and **merges** closed cases into the Journal; the Planning Desk **pins** a scenario snapshot to a case (a frozen result view model — ADR-0107 Decision 6, nothing recomputed); and Shirley gains a per-session **case brief** plus a **consultation-excerpt pin**. **The seven→eight documentation reconciliation ran in this same change** — `CLAUDE.md`, `docs/architecture.md`, and `readme.md` move from seven Areas to eight (migration head → `b031`; Case glossary/model rows; Cases modules/routes and a schema-narrative paragraph), discharging the ADR-0107 "with the implementation, not before it" commitment. **Gate evidence recorded in the closure note §6**; the strand's ID-less follow-ups remain in the closure note's registers. **#049 unchanged** — it stays `open (#048 shipped)`, the Strand-2 §10 gates still pending (operator action). Next free ID → **#052**. |
| 2026-07-20 | **#051 raised** — Case Workflow: the Cases area (P1, open, ADR-0107). The roadmap entry ADR-0107 §Implements called for on filing; it was filed with the ADR but the entry itself had not been created, so **#050** (the watch family the same ADR *commissions*) was on file while the item ADR-0107 *is* was not. Successor of the "Execution Network" concept kernel, whose provider-directory half is cut entirely. It adds the **eighth** Area — the third such addition after Decision Console (ADR-0089) and Planning Desk (ADR-0104 §6) — so CLAUDE.md, `docs/architecture.md`, and the ADR-0084 glossary all move from "seven Areas" to "eight" **with the implementation, not before it**. Next free ID → **#052**. No other item state changed: **#049** was left `open (#048 shipped)` deliberately, per the Strand 3+4 closure §8 — the flip to Shipped waits on the Strand-2 §10 gate evidence, which is not yet on file. |
| 2026-06-03 | Restructured the roadmap into the **Loose ends / Features** model with a passive **Shipped** record, retiring the migration-era A/B/C/D taxonomy (Deep Dive / New Features / Infrastructure / Quick Wins) and the Mission-Control narrative. Translated to English (ADR-0008 — removing the last German-doc exception). Assigned flat, category-independent IDs (`#001`…) in old-category order; added the full old→new crosswalk. No item state changed beyond the mechanical mappings: legacy `mostly-done` (B5, B8) mapped to `in-progress`; all Demo-path flags defaulted to `no`. Restructure and translation only — not a re-audit. |
| 2026-05-12 | Initial version after the Phase-6 close-out. Roadmap starts with A6a/D1 already `done` (completed on the Phase-6 closure path). A6 split into A6a-lite (done) and A6b-full (open). A9 (Portfolio-Review filter) added as a new item. A7 description refined after the 6F-3d closure (single-investment surface stays in the old layout). |
| 2026-05-17 | A5 (SAA Back Office) verified `done` (ADR-0054). Removed from the ordering recommendation. |
| 2026-05-20 | Roadmap sync after a doc audit: A2 (Report Scraper) and A4 (Shirley base migration) verified `done` (ADRs 0053, 0048–0052). D3 (Block-0 language hygiene) verified `done`. New item B5 (Anlagegrenzen monitoring, Phase 7) added — data layer complete (ADRs 0055/0056/0057, migration b010), engine/import/surface open. Ordering recommendation adjusted. |
| 2026-05-20 | B5 updated to `mostly-done` after completing Kickoff #1 (Excel import), #2/#3a (engine in `services/analytics/limit_coverage.py`), and #3b (read-only web surface at `/back-office#limits`). B5a/B5b/B5c each `done`; B5d (Portfolio-Review integration) stays `open`. |
| 2026-05-24 | A12 (Benchmarks & Attribution, Phase 1) added as a new item with status `in-progress`. Implementation follows the three-prompt atomicity convention (schema/import/extractor, analytics, service+UI) against ADR-0061. |
| 2026-06-01 | A7 (single-investment Portfolio-Review surface) set `done` (ADR-0069): per-investment reviews as a continuous, lazy-loaded stack in the Portfolio-Review section, orphan template deleted. A1 PDF export stays `open`, render target Path B (`kaleido` over the bundle contract) documented as an advance decision. |
| 2026-06-03 | Doc/code reconciliation pass (ledger: `docs/_audit/doc-code-reconciliation-2026-06-03.md`). A8 (Statistics latency) verified `done` (N+1 query fix `5682772` + ADR-0065). A9 scope sharpened: the `investment_ids` backend filter already exists; only UI + route wiring + URL reflection remain. B8 split to `mostly-done`: dynamic tool list `done` (2026-06-02, `e2af923`), dataset context `open`. Two missing `done` entries added: A14 (Front-Office Overview chart row + fund-composition Pareto, ADR-0072) and B10 (Shirley back-office analysis tools Phase 1, ADR-0069). Identifiers A14/B10 provisionally assigned (renumberable). ADR-0069 number collision resolved: single-investment-review ADR renumbered 0069 → 0073 and set to `Accepted` (A7 is shipped); back-office analysis tools keeps 0069. ADR-0063 and ADR-0064 corrected from `Proposed` → `Accepted` (code shipped: b012/b013/b014). |
| 2026-06-04 | Recorded two shipped multimodal capabilities that post-dated the 2026-06-03 restructure and were never tracked as items: **#029** Multimodal image input for Shirley (ADR-0075) and **#030** Voice I/O for Shirley (ADR-0076), each implemented across the web and Telegram surfaces (three blocks each), added directly to the Shipped record. Next free ID → #031. ADR-0075 and ADR-0076 set Proposed → Accepted. |
| 2026-06-16 | Added **#031** Liquid-archetype test-data fidelity follow-ups under **Loose ends** (P3, open) — the five imprecisions accepted by ADR-0081's Variante-A choice (NAV-preserving income split), each phrased as a concrete future action. Cross-references ADR-0081. Next free ID → #032. Documentation-only step ahead of the importer extension and the v26 workbook (later prompts). |
| 2026-06-16 | Added **#032** Regulatory Reporting Pre-Fill (BaFin/BerVersV quarterly returns) under **Features** (P2, open) — pre-fill-not-system-of-record scope for the six supervisory returns; spine already present (AnlV classification, NAV, AUM, limit-coverage engine), gaps documented (book value, SV split, issuer/LEI, derivatives, fund Durchschau). Precondition #032a (AnlV-taxonomy correction) landed via ADR-0083. Next free ID → #033. |
| 2026-06-28 | Extended **#023** (Cashflow / Exposure Projection) to name the **Takahashi–Alexander** parametric model as the canonical forecast engine and to add an easy-to-operate **transformation surface** (pacing / scenario shifts feeding the same pure engine). Added two new **Features**: **#033** Decision Console (proactive heartbeat agent / Irene — read-only data access, delta detection against a decision journal, edge-triggered `surface_finding`, urgency 1–10 bands; the hard part is materiality calibration over time) and **#034** Scenario Analysis (economic-downturn / rate / stress regimes as a pure analytics module consuming #023's projection). Both carry a concept ADR as their first kickoff deliverable. Next free ID → #035. |
| 2026-07-02 | Qt sunset (ADR-0094 Stage 1) reconciliation. **#016** (GUI Migration & Qt Sunset) set `done` and moved to the **Shipped** record: the `gui/` surface, `main.py`, the Qt AIService adapter and its `services/ai_service.py` shim, and the PyQt6 and Qt test dependencies were removed, and the former top-level `analytics/` package was folded into `services/analytics/`. Added **#035** DataStore complex decommission (Qt-sunset Stage 2) under **Loose ends** (P2, open) per ADR-0094 §5 — the in-memory DataStore complex the GUI left behind, deferred because of live web-side consumers (`load_excel`, `compute_irr`); dependencies: Category-A review item A5 (`load_excel` relocation) and #001. Next free ID → #036. Documentation-only pass; no code changes in this step. |
| 2026-07-03 | Doc/code reconciliation after the Irene / Decision Console landing (migration `b019`; ledger: `docs/_audit/doc-code-reconciliation-2026-07-03.md`). **#033** Decision Console (proactive heartbeat / Irene) set `open` → `in-progress` with ADR references 0085–0089; the first slice — two-table append-only persistence, delta engine, edge-triggered beat, non-streaming `surface_finding` synthesis contract, out-of-process CLI tick adapter + systemd units, and the Decision Console web surface (the sixth Area, ADR-0089) — is implemented and tested, while the full proactive agent (materiality calibration over time) stays open. Added **#036** Live Data Import (provider-agnostic market-data ingest) under **Features** (P2, open, ADRs 0090–0093) — accepted design, no code yet. Next free ID → #037. `CLAUDE.md` and `docs/architecture.md` reconciled to **six** Areas (Decision Console added, ADR-0089) and to migration head `b019`; new `services/irene/`, `services/voice/`, `services/front_office_charts/`, and `modules/decision_console/` rows added to the architecture tables. Documentation-only pass; no code changes. ADRs 0085–0089 are eligible for Proposed → Accepted (operator action, outside this pass). |
| 2026-07-06 | **#036** Live Data Import: ADRs 0090–0093 accepted (status `Proposed` → `Accepted` in each ADR body and the `docs/adr/README.md` index; README prose note updated). **#036** set `open` → `in-progress` (summary row + detail block). Implementation slice 1 started: the `investment_identifiers` table (migration `b020`, per ADR-0090 §Decision — dedicated identifier table keyed on `(investment_id, scheme, value)` with a partial per-tenant uniqueness rule for real-world schemes and a one-primary-per-investment guard), its ORM model, and repository. The market-data provider port (ADR-0091), live-ingest contract (ADR-0092), and out-of-process tick adapter (ADR-0093) remain unimplemented. |
| 2026-07-06 | **#036** Live Data Import slice 2 landed — **Excel identifier ingestion** (ADR-0090 §"Identifiers enter through both import paths"). The `InvestmentExtractor` now parses the optional `ISIN` / `Ticker` Attributes rows into `ImportedIdentifier` tuples (permissive: blank cells yield no rows, no checksum/format validation); `InvestmentService.transform_upload_to_investments` gained an opt-in `investment_identifier_repository` that reconciles the `source='excel'` subset per investment (excel rows absent from the workbook deleted, new ones inserted with `source='excel'`, `openfigi`/`manual` rows never touched, all deletions before any insertion) and promotes ISIN-else-ticker to primary when an investment has none. Added a minimal `InvestmentIdentifierRepository.set_primary` update path for the promotion. Also hardened `services/reporting/attributes_partition.KNOWN_SCALAR_ATTRS` with the English scalar variants (`Currency`, `Manager Name`) and the two identifier rows (`ISIN`, `Ticker`) so trailing identifier rows cannot corrupt the sector/country breakdown detection. **#036** stays `in-progress`. Outstanding operator action for this slice: the full `pytest` run (this session ran only the new + directly touched suites per the time-boxed instruction) plus a browser import check of the v29 workbook, both before commit. OpenFIGI / FIGI derivation (ADR-0091), the market-linked predicate helper, identifier CRUD web surface, and `ingest_origin` (ADR-0092) remain later slices. |
| 2026-07-06 | **#036** Live Data Import slice 3 landed — the **market-data provider architecture** (ADR-0091). New `services/market_data/` package (parallel to `web_research/` / `voice/`, never under `analytics/`): an async `MarketDataProvider` port with a small port-error hierarchy; the provider-blind `NormalizedSeries` / `NormalizedQuote` DTO defined around the canonical target tables (`SeriesKind` = `nav_price` + the seven `flow_type` cashflow kinds + the five historised composition-weight families), with strict construction validation (Decimal-only, strictly-ascending dedup dates, closed kind set, identifier normalisation); a declarative capability matrix (`config/market_data_capabilities.yaml`) driving a priority-routing factory; and three adapters — `yahoo` (native-async httpx: unadjusted EOD close as `nav_price` + dividend events, gmtoffset statement-day dates; empirically ticker-only, price+dividend-only, encoded in the matrix), `synthetic` (fixture-driven, fully deterministic test-event injection seam), and deterministic OpenFIGI ISIN/ticker→FIGI normalisation (`normalisation.py`, no persistence). A new purity guard `tests/regression/test_market_data_layer_pure.py` forbids SQLAlchemy / FastAPI / Qt / repository / LLM imports under the package. `.env.example` gained optional `OPENFIGI_API_KEY`; operator sample fixture at `config/market_data_synthetic_example.json`. This slice is code-architecture only: no migration, no DB write, no `InvestmentService` change, no tick/web surface. **#036** stays `in-progress`. Outstanding operator action: the full `pytest` run (this session ran only the new suites + the two directly-touched regression guards per the time-boxed instruction) before commit; an optional manual live smoke against Yahoo/OpenFIGI. The live-ingest write path (ADR-0092), tick adapter (ADR-0093), FIGI persistence, and identifier CRUD surface remain later slices. |
| 2026-07-06 | **#036** Live Data Import slice 4 landed — the **live-ingest contract and Excel precedence** (ADR-0092). Migration `b021` adds a typed `ingest_origin TEXT NOT NULL CHECK (IN ('excel','live','manual'))` to all **seven** ingested tables (`investment_navs`, `investment_cashflows`, and the five historised composition-weight tables), backfilled to `'excel'` via an add-with-server-default → NOT NULL → drop-default sequence so every application write states its origin explicitly; and adds a nullable `investment_cashflows.source` column. A new pure, DB-free `services/investments/cashflow_dedup_key.py` computes a deterministic SHA-256 dedup key over `(investment_id, flow_timestamp[UTC], flow_type, flow_kind, amount[Decimal.normalize], source[None-sentinel])` (guarded by `tests/regression/test_cashflow_dedup_key_pure.py`, mirroring the Irene key-forming discipline). The Excel-precedence guard is a conditional upsert (`InvestmentNavRepository.upsert_live` + representative `InvestmentRegionWeightsRepository.upsert_live`: `ON CONFLICT DO UPDATE … WHERE existing.ingest_origin = 'live'`) so a live write overwrites no `'excel'`/`'manual'` row (left byte-identical), refreshes only its own prior `'live'` rows, and inserts where the book of record is silent. `InvestmentService.ingest_normalized_series` consumes a provider-blind `NormalizedSeries`/`NormalizedQuote` for a known investment and routes `nav_price` (→ NAV, `basis='reported'`) and the seven cashflow kinds (→ cashflow, dedup-keyed, sign trusted from the adapter per ADR-0091 §3), returning a frozen `LiveIngestReport` (`inserted`/`updated_live`/`skipped_excel`/`skipped_manual`/`noop_live`) and logging one structured INFO line; re-running a series is a no-op (idempotency). The Excel transform and the four manual CRUD writers now state their origin (`'excel'` / `'manual'`). **Three operator-approved ADR-gap resolutions, implemented and flagged:** (1) `b021` adds the missing `source` column to `investment_cashflows` (ADR-0092's dedup key names it but the table lacked it; NULL is the honest pre-live value); (2) the conditional upsert leaves both `'excel'` **and** `'manual'` rows live-immune (the `WHERE ingest_origin='live'` formulation), reported as separate `skipped_*` counts; (3) manual CRUD (`add_nav`/`update_nav`/`add_cashflow`/`update_cashflow`) writes `'manual'`, while Excel re-import stays unguarded (book of record). **Known DTO gap (flagged):** `NormalizedSeries` carries only `(as_of_date, value)` per point with no bucket dimension, so `weight_*` kinds cannot yet be routed through the service (raises `NotImplementedError`); the row-level weight guard exists on the region repository as the representative and awaits a bucketed-weight DTO (services/market_data unchanged this slice). **#036** stays `in-progress`. Outstanding operator action: the full `pytest` run (this session ran only the new suites + the directly-touched suites per the restricted-scope instruction) before commit. The tick adapter (ADR-0093), FIGI persistence, and the identifier CRUD surface remain later slices. |
| 2026-07-07 | **#036** Live Data Import slice 5 landed — the **trigger and out-of-process tick** (ADR-0093), mirroring the Irene tick topology 1:1 (ADR-0086). Migration `b022` adds `market_data_schedule` (the market-data analogue of `irene_schedule`: tenant-scoped, nullable per-user seam, `enabled` / cadence / `next_due_at` / `last_run_at`, RLS via `apply_tenant_rls`) — `enabled` defaults **FALSE** so no fresh tenant silently fetches. New pieces: the market-linked predicate helper (`services/investments/market_linked.py`, ADR-0090 — `listed_equity`/`listed_bonds` with a primary `isin`/`ticker`/`figi`); the per-tenant refresh core (`services/investments/live_refresh.py`) that resolves eligible investments, drives the fetch-kind list off the capability matrix, fetches over a window from the last successful run (30-day fallback), and writes via `ingest_normalized_series` attributed to the tenant system actor, with per-investment error containment and a structured per-tenant INFO log; the cross-tenant due read (`services/investments/live_schedule.py`); `MarketDataScheduleRepository`; the `portfoliflow market-data-tick` CLI (no AI/LLM dependency; `--tenant` / `--provider` test-seam flags that do not persist schedule state); systemd units (`docs/deploy/market-data-tick.{service,timer}` + README); and a small Admin web surface (`/admin#market-data`) for cadence CRUD + "Refresh now" (`next_due_at := now`, no provider work in the request). **Four operator-approved decisions (§0), implemented and flagged:** (1) a per-tenant **system actor** (`market-data-service@service.portfoliflow.invalid`, `is_active=False`, display name "Market Data Service") seeded through `seed_tenant_defaults` **and** bootstrap with ADR-0077 parity — the schema CHECKs forbid the literally-"empty" roles and a NULL password, so it carries the least-privileged `auditor` role (inert while inactive) and an unusable hash of a random secret; (2) **advisory-lock domain separation** — `advisory_lock_key` gained a `domain` parameter, keeping every Irene key byte-identical (pinned in a test) while the tick claims a disjoint `market_data`-domain key; (3) **"Refresh now" = `next_due_at := now()`** — the web action only sets the schedule due, the tick does the work; (4) **CLI test seam** — `--tenant` / `--provider`, non-persisting. Rides along: the `weight_*` kinds were removed from the `synthetic` capability-matrix entry (the ingest cannot route weight kinds until a successor ADR extends the DTO with a bucket dimension), with the factory test pin adjusted. `.env.example` gained `MARKET_DATA_SYNTHETIC_FIXTURE=`. **#036** stays `in-progress`. Deferred and still open under #036: the Bloomberg adapter (entitlement-gated), FIGI persistence + the identifier CRUD surface, and the weight-DTO successor ADR. Outstanding operator action before flipping #036 → shipped: the full `pytest` run (this session ran only the new + directly-touched suites per the restricted-scope instruction), a v29 browser import walk, a live Yahoo/OpenFIGI smoke, and the synthetic end-to-end loop (`market-data-tick` → `irene-tick` → Decision Console). |
| 2026-07-07 | **ADRs 0095/0096 accepted; roadmap #037 raised; Stage-1 credential resolver landed.** ADR-0095 (Provider Credential Vault) and ADR-0096 (Identifier Scheme-Set Extension) set `Proposed` → `Accepted` (ADR bodies + `docs/adr/README.md` index rows and prose note; next free ADR number → **0097**). New **#037** Provider Credential Management raised under **Features** (P1, open, ADR-0095) — Stage 2 is the tenant credential vault: a `provider_credentials` table with RLS, application-level **Fernet** encryption keyed by `CREDENTIAL_VAULT_MASTER_KEY` (never stored in the DB), a re-encrypt rotation CLI, and a write-only/masked tenant-admin management surface; depends on **#015** (Multi-User & Permissions) for the tenant-admin authorisation model. **Stage 1 delivered now:** the `CredentialResolver` seam (`services/investments/credential_resolver.py` — environment-only source, explicit ADR-0095 §1 resolution order, per-provider fallback policy) with the policy declared in `config/market_data_capabilities.yaml` (`yahoo`/`synthetic` = `none`, `openfigi` = env-fallback-allowed + `optional`); the OpenFIGI key read removed from `services/market_data/normalisation.py` (now credential-source-blind — the key is an injected parameter). Under **#036**, the ADR-0096 scheme-set-extension + identifier-CRUD slice is recorded in a new **Deferred items (tracked)** list. **Discrepancy resolved:** the prompt assumed `resolve_figi` had a call site (`live_refresh.py`) to rewire; it has none yet (FIGI resolution is an unlanded later slice), so no call site was fabricated — the resolver is ready for when FIGI resolution is wired in. Next free ID → **#038**. Documentation + Stage-1 code; restricted test scope (new + directly-touched suites only). |
| 2026-07-07 | **#036** Live Data Import — the **ADR-0096 scheme-set extension + identifier CRUD surface** landed (migration `b023`). Migration `b023_extend_identifier_schemes` swaps the `ck_investment_identifiers_scheme` CHECK from the ADR-0090 five-scheme set to the ADR-0096 seven-scheme set (`isin`,`ticker`,`figi`,`cusip`,`internal`,`preqin`,`pitchbook`) — a widening constraint swap, no other schema change; the three ADR-0090 uniqueness rules carry over unchanged and are exactly right for provider IDs. The two code frozensets (`core.models.investment_identifier.IDENTIFIER_SCHEMES`, `services.market_data.dto.IDENTIFIER_SCHEMES`) were extended in the same commit and are now pinned together with the migration CHECK literal by `tests/regression/test_identifier_scheme_set_consistency.py`. `_SCHEME_TO_ID_TYPE` (OpenFIGI) is deliberately left unchanged — provider-native schemes are not FIGI-resolvable (ADR-0096 §1), pinned by a new `resolve_figi('preqin', …)` → `UnsupportedCapabilityError` test. New thin `InvestmentService` methods (`add_identifier_manual` [`source='manual'`], `set_primary_identifier` [demote-then-promote in one transaction], `delete_identifier` [deleting the primary is allowed and lapses eligibility, no auto-promotion], `list_identifiers`) back a **Security Identifiers** panel on the investment detail page with three nested-resource routes (`POST /investments/{id}/identifiers`, `POST …/{identifier_id}/primary`, `DELETE …/{identifier_id}`) mirroring the NAV/cashflow idiom (owner-only, CSRF, cross-tenant→404). Provider-native IDs enter only through this **human-confirmed** surface; no auto-matching/import path writes a provider-ID mapping (ADR-0096 §2). **Deviation flagged:** to give the service a demote primitive, `InvestmentIdentifierRepository.set_primary` gained a backward-compatible `is_primary: bool = True` kwarg (touches `core/repositories/`, outside the prompt's stated file list, but §3.2's demote-then-promote is unsatisfiable otherwise without a raw-ORM write in the service). **#036** stays `in-progress`; remaining deferred items restated (Bloomberg adapter — entitlement-gated; FIGI persistence; weight-DTO successor ADR; predicate generalisation per ADR-0096 §3, trigger: first private-markets adapter live). Restricted test scope (new + directly-touched suites only); full `pytest` run + a browser walk of the new panel remain operator actions before commit. |
| 2026-07-07 | **#036** Live Data Import — the **Bloomberg Desktop-API adapter** landed **fixture-validated** (ADR-0091, deferred-items track). New `services/market_data/adapters/bloomberg.py`: an async `BloombergAdapter` that satisfies the `MarketDataProvider` port over a **synchronous `BloombergGateway` seam** (plain-dict requests in, event-flattened dicts out), bridging at exactly **one** `asyncio.to_thread` site (the single bridge the ADR promises). Security topics are rule-formed — `figi` → `/bbgid/<FIGI>`, `isin` → `/isin/<ISIN>` (`ticker` unsupported: the yellow-key suffix is unstored, guessing it would break key-forming discipline). `nav_price` maps daily `PX_LAST` from a `HistoricalDataRequest` (holiday date-only rows skipped); the series currency is read from a separate `CRNCY` `ReferenceDataRequest` — never guessed (empty `CRNCY` → `ProviderFetchError`). Error mapping onto the port: `securityError` → `IdentifierNotResolvableError`; `responseError` / field exceptions / gateway session-service failures → `ProviderFetchError`; unsupported scheme/kind → `UnsupportedCapabilityError`; no `blpapi` type crosses the boundary. Only the real `BlpapiDesktopGateway` imports `blpapi` — **lazily, inside a method** (per-fetch session lifecycle; no pooling in v0) — raising `MarketDataConfigurationError` with Bloomberg's own pip-index install hint when absent, so the whole suite passes on a machine without `blpapi` (it is NOT on public PyPI). **Four operator-approved §0 decisions, implemented and flagged:** (1) schemes `figi`/`isin` only, no `ticker`; (2) the capability matrix gained an optional per-provider **`enabled`** flag (boolean, default `true`; a disabled provider is dropped from routing = "not declared"), and the provider-entry parser now rejects unknown keys; (3) the Desktop-API variant declares `credentials: none` — the Terminal session is the auth/entitlement boundary, host/port (`BLPAPI_HOST`/`BLPAPI_PORT`, defaults `localhost:8194`) are env-read by the **factory** (synthetic-fixture-path precedent), so the adapter stays credential-source-blind; the Server-API/B-PIPE/Data-License variants stay #037-gated (`env_fallback: forbidden`, future separate adapters); (4) a **gateway seam** for testability — tests drive a fake gateway, never a mocked `blpapi`. The `bloomberg` matrix entry ships **`enabled: false`** (priority 200, above yahoo; disjoint scheme sets so no overlap today) plus a `bloomberg: none` credential policy. **`dividend` deferred (honesty gate, §1.5):** the `DVD_HIST_ALL` dividend-amount currency (declaration currency vs. `CRNCY`) is a material ambiguity unresolvable from the docs without a live Terminal, so the adapter ships `nav_price`-only and `dividend` is left out of the matrix entry — open question recorded for the live-smoke step. **Zero downstream change** (ADR-0091's claim, proven by file list): nothing under `services/investments/`, `core/`, or `db/` was touched — one adapter file plus one matrix entry. `.env.example` gained a Bloomberg block; `docs/deploy/README-market-data-tick.md` gained a Bloomberg subsection. **#036** stays `in-progress`; the **live smoke against a real, entitled Terminal remains the gated activation step**. Restricted test scope (new + the directly-touched market-data / live-refresh suites only, run on a machine without `blpapi`); full `pytest` run remains an operator action before commit. Activation checklist: install `blpapi` from Bloomberg's index on the Terminal machine, set the env vars, flip `enabled: true`, run a `--tenant`-scoped manual tick as the live smoke, then rely on the timer path. |
| 2026-07-08 | **#038 Position Model raised** (ADR-0097 / ADR-0098, both Accepted). Strand-0 architecture chat verified the latent P0 (per-share prices in position-level NAV series, finding F1) and its dividend sibling (F6) against the snapshot and recorded the guard note on the item. Strand table S0–S5 frozen; S0 (interim ingest guard) is ship-immediately. #038 blocks Planning Desk, #034, #032 implementation, and live-ingest activation for listed instruments. |
| 2026-07-09 | **#038 Position Model: strands S0–S5 landed** (ADR-0097/0098, Accepted). S1 `b024` (position_transactions, instrument_prices, investments.valuation_mode, RLS), S2 `b025` + synchronous computed-NAV materialisation ('system' origin), S3 mode-aware live-ingest re-routing (F1/F6 structurally closed; S0 interim guard retired), S4 Excel v30 Units rows → 'excel' opening synthesis, S5 positions surface + one-way flip route. #038 → in-progress (operator walkthrough pending; synthetic unitisation deferred per ADR-0097 §8). P0 guard note marked resolved. Fixed pre-existing shell-catalogue omission (admin `market-data` section, #036 fallout) — section nav/command search regression guard green again. Raised **#039** migration-roundtrip test-design fix under Loose ends. Next free ID → #040. |
| 2026-07-11 | **Multi-currency programme landed** (ADR-0099 / ADR-0100 / ADR-0101; blocks 1–5; migrations `b026`, `b027`) — recorded retrospectively: the programme ran without a roadmap ID, and its deliberately deferred items lived only inside the ADRs' Out-of-scope / Follow-up sections. **What landed.** ADR-0099: `tenants.functional_currency`, the tenant-scoped `fx_rates` table quoted against a reference currency, the pure `services/fx/` conversion service (identity short-circuit, triangulation, ADR-0060-style carry-forward, typed `MissingFxRateError` — no silent 1:1 fallback anywhere), and the single conversion boundary in front of the analytics layer, which keeps ADR-0013 purity intact and hands analytics a *stronger* single-currency contract; the Excel `FX rates` market-reference sheet is the v1 supply path (migration `b026`). ADR-0100: `'cash'` as the **eighth** `investment_type` (migration `b027`), explicit foreign-currency cash rows as ordinary investments (converted, limit-checked, AnlV-classifiable at no new machinery), the per-engine contracts (cash **in** NAV aggregation / AUM coverage / limits / composition, **out** of IRR / TVPI / DPI at the data-assembly seam), and the **redefinition of the ADR-0055 residual** — retained as a formula but narrowed in meaning to *cash in functional currency*, since explicit cash rows now sit inside `Σ nav_functional` and shrink the residual by construction. ADR-0101: the currency-exposure donut as the fourth Overview chart tile, the FX-cash card, and currency-aware money labels (`_format_money_compact`) — all conditionally rendered, so a single-currency tenant's Overview is byte-for-byte what it was. **Test data:** workbook bumped to **v31** — an `FX rates` sheet, sample Investment T relabelled `Money Market`, and a new `Cash USD` column. **Alias retarget (flagged):** the extractor's `Cash` type label now maps `cash → cash`, **superseding ADR-0081 §3**, which had pointed it at `listed_bonds` "rather than introducing a fourth archetype now"; ADR-0100 §5 is that "now" and rules the underlying instrument — a money-market fund is a *fund*, so the sample MMF keeps `listed_bonds` through its relabelled `Money Market` cell, while `Cash` (and the German `Kasse` / `Liquidität`) resolves to the new type. This also settles half of #031's point 3 (the `credit` alias remains to be re-examined against real GP data). **Four deferred items raised** as Loose ends, lifted out of the ADR bodies so they stop being invisible: **#040** Statistics / SAA currency contract (P2 — `services/portfolio_analysis/` still measures nominally and now visibly disagrees with the converted Review KPIs; ADR-0099 §6), **#041** functional-currency field renames `*_eur` → `*_functional` (P3 — values and labels are correct, only the identifiers still say EUR), **#042** live FX-rate supply (P3 — Excel-only today; `SeriesKind.FX_RATE`, a capability entry, and an ECB SDMX adapter are absent, while `FxRateRepository.upsert_live` sits dormant), and **#043** glossary v3 currency terms (P3 — *functional* / *position* / *reference* currency and *explicit cash position* vs. *cash residual*). **Deliberately not raised:** FX hedging / currency overlay (a future *capability*, not an unfinished state — ADR-0099 §6; #034's scenario notes already touch FX) and overdrafts / negative cash balances (ADR-0100 §5, out of scope by design). Next free ID → **#044**. Documentation-only pass; no code changed in this step. |
| 2026-07-11 | Doc/code reconciliation of the three steering documents to Alembic head `b027` — the multi-currency wave (migrations `b026`/`b027`, ADRs 0099–0101) had landed without a reconciliation pass (ledger: `docs/_audit/doc-code-reconciliation-2026-07-11.md`). **`CLAUDE.md`:** migration-head sentence → `b027`; `investment_type` corrected from **seven** to **eight** canonical values (`'cash'`, ADR-0100 §1) in both the Investment and Investment-Type glossary rows; a `services/fx/` sentence added to the architecture paragraph (pure conversion service at the single ADR-0099 §4 boundary, no silent 1:1 fallback, ADR-0013 purity preserved); and **glossary v3** added — *functional currency*, *position currency*, *reference currency*, *explicit cash position*, *cash residual* — with the `AUM` row reconciled so its pre-ADR-0100 "cash is not a persisted entity" claim no longer contradicts them. **`docs/architecture.md`:** three head mentions → `b027`; new `fx/` services row; `market_data/` extended (Bloomberg Desktop-API adapter, per-provider `enabled` flag and credential policy); `investments/` extended (`live_refresh`, `live_schedule`, `market_linked`, `credential_resolver`, `cashflow_dedup_key`); `decision_console` + `market_data` added to the `web/routes/` list; seven → eight investment types in the schema vocabulary and the investment-domain narrative; and a multi-currency paragraph appended to the schema narrative. **`docs/roadmap.md`:** **#039** (migration-roundtrip test design) set `done (2026-07-10)` and moved to **Shipped** — commit `5599175` (Multi-Currency Block 1) converted all five guards (`b021`, `b022`, `b024`, `b025`, liquid-archetype `b016`) to pin their **named** target revision and restore `alembic upgrade head` in a `finally` block, so a new migration no longer re-targets and breaks the previous migration's guard; **#043** (glossary v3: currency terms) set `done (2026-07-11)` and moved to **Shipped**, closed by the `CLAUDE.md` glossary-v3 edit in this very pass; **#036**'s internal contradiction resolved — the Scope sentence claimed "no code has shipped yet" while the change log recorded five landed slices, so Scope and the status heading now state what actually shipped (slices 1–5, the ADR-0096 identifier surface, Stage-1 credentials, the fixture-validated Bloomberg adapter), with the live smoke and the Deferred-items list as the open remainder (**#036 stays `in-progress`**). ADR status flips (0085–0089/0094/0099–0101 → Accepted) were done separately and are not part of this pass. Documentation-only; no `.py`, template, or config changed. |
| 2026-07-11 | **#040 Statistics / SAA currency contract landed** (ADR-0102, Accepted) — `done (2026-07-11)`, moved to **Shipped**. **The decision.** ADR-0102 is the successor ADR-0099 §6 deferred, and it settles the one substantive question that deferral named: **measurement is in the tenant's functional currency, FX effect included** — the same choice ADR-0099 §4 already made for the review path's IRR/TVPI/DPI, so the efficient frontier is optimised on functional-currency returns, volatility and the correlation matrix are computed on them, and levels (totals, current-portfolio weights, composition, composite NAV weights) become consistent with Review/Overview by construction. **What landed.** `PortfolioAnalysisService`, `StatisticsService`, and `BenchmarkComparisonService` now convert at the **existing** ADR-0099 §4 boundary via the same `build_portfolio_fx_converter` / `convert_series` idiom — callers added to the seam, not a variant of it (one conversion idiom, five callers) — each taking `TenantRepository` + `FxRateRepository` and building one converter per request; `BenchmarkComparisonService` converts at all three assembly sites uniformly, so there is no per-site currency exception. **Analytics purity intact:** no function under `services/analytics/` changed; `test_analytics_layer_pure.py` stays green — a seam change, not an engine change. **Error states:** `web/routes/statistics.py`, `web/routes/portfolio_analysis.py`, and `web/routes/benchmarks_attribution.py` catch `MissingFxRateError` and render an HTTP-200 error partial for the HTMX section swap (`statistics_error.html`, `portfolio_analysis_error.html`, `benchmarks_attribution_error.html`), on the `overview_error.html` / `limits_error.html` precedent — **still no silent 1:1 fallback anywhere**. **Invisibility preserved:** a single-currency tenant hits the zero-read fast path, every conversion is an identity pass-through, and each section carries a regression test mirroring `test_single_currency_tenant_sees_no_fx_surfaces` — byte-for-byte unchanged. The two halves of the app now agree on any mixed-currency book (v31 sample data included), which was the demo failure the item existed to remove. **#045 raised** under **Features** (P3, open, ADR-0102 §1): FX / asset-return attribution — decompose the functional-currency return into asset-performance vs. currency components; **the local-currency perspective lives there**, deliberately not bolted onto ADR-0102 as a parallel measurement basis (Alternatives Option C — half an attribution model is a confusing partial). It owns two open decisions: the multi-period linking convention (Carino / Menchero vs. arithmetic split) and the disclosure surface. **`#044` is unissued** — ADR-0102 names the follow-up `#045` in four places and the ADR wins on conflict, so the number was skipped rather than the ADR rewritten; recorded as a permanent hole under §ID scheme. **#041** (`*_eur` renames) stays open and proceeds next, against the converted services; **#042** (live FX supply) is un-precluded. Next free ID → **#046**. |
| 2026-07-11 | Doc reconciliation to the shipped ADR-0102 behaviour (this pass, documentation-only). **`docs/adr/README.md`:** index row for **0102** added (Accepted, 2026-07-11, tags analytics/fx/currency/statistics/saa/benchmark/engine-contract/phase-8); next free ADR number **0102 → 0103**; the trailing prose note extended with the 0102 acceptance and the #045 pointer. **ADR-0102 front-matter flipped `Proposed` → `Accepted`** against the shipped code (commits `93e16de`, `fb1de62`), with a Revision-History row appended — the 0099/0101 flip precedent; the ADR *body* was not otherwise touched. **`docs/architecture.md`:** the `portfolio_analysis/`, `statistics/`, and `benchmark_comparison/` services rows now state that each converts into the functional currency at the ADR-0099 §4 boundary via `build_portfolio_fx_converter` (ADR-0102 added to their ADR column). **Two stale enumerations fixed beyond the prompt's scope:** the `fx/` services row and the multi-currency schema narrative both listed the boundary's consumers *exhaustively* as "`PortfolioReviewService` and `LimitsCoverageService`" — true under ADR-0099, false since ADR-0102 — so both now name all five callers, and the narrative records the functional-currency measurement basis, the FX-inclusive returns, the HTTP-200 error-partial behaviour, and the #045 pointer. **`CLAUDE.md`:** no standalone "converted surfaces" list exists, but the glossary-v3 **Functional currency** row carried the same exhaustive enumeration ("every aggregate *the review and limits seams* publish"), so it now names the statistics / portfolio-analysis-SAA / benchmark sections too and records that they measure *returns* in the functional currency, FX effect included (ADR-0102 §1). Nothing else in `CLAUDE.md` needed touching: the architecture paragraph describes `services/fx/` as sitting at the single boundary without naming consumers, and the ADR topic table has never carried multi-currency rows. No test-suite change: the repository has no docs-link or roadmap-consistency check to run. |
| 2026-07-11 | **#027 `db/init/` mechanism documented** — `done (2026-07-11)`, moved to **Shipped**. Closed in `db/README.md` (a new **`## The init/ directory`** section) rather than in the new `docs/development.md` the item speculatively proposed: the README already owns the roles / migrations / reset context the question arises in, and a second development doc would have split it. The section states what `01-create-app-role.sql` actually does (the unprivileged `portfoliflow_app` role — no `SUPERUSER`, no `BYPASSRLS` — plus `CONNECT` / schema `USAGE` and the `ALTER DEFAULT PRIVILEGES` grant that makes the superuser-owned, Alembic-created tables reachable without a per-migration `GRANT`); **when it runs** — from `/docker-entrypoint-initdb.d`, into which `compose.yml` mounts `./db/init` read-only, executed by the Postgres entry point exactly once, on the first start against an **empty** data volume, in filename order (hence `01-`), and **never again** — which is the Lesson-#7 gotcha the item was raised for: a role change on a live dev DB needs either hand-run SQL or a full reset; **how to re-trigger it** — `scripts/db-reset.sh` (volume teardown → fresh init → `alembic upgrade head` → bootstrap), explicitly contrasted with `portfoliflow reset-dev --confirm`, which truncates domain data on the *existing* volume and re-runs neither `init/` nor the migrations; and the **division of labour** — `init/` owns cluster-level bootstrap (roles) only, Alembic owns all schema, so new DDL must never land in `init/`, where it would silently skip every database whose volume already exists. `docs/architecture.md`'s `db/` paragraph already said the role is created "on the first Postgres start" but not that it never re-runs, so it gained one sentence carrying that property and pointing at `db/README.md`. Documentation-only; no SQL, compose, or code change (deliberately: the dev-only password literal in the init SQL and its Phase-5 secret-manager note are untouched, and belong to #025). |
| 2026-07-11 | **Four operator decisions on the Portfolio-Review / limits / Shirley-grounding front** (documentation-only; no code changed). **(1) B5d rejected — won't-do.** The limit-coverage tile in the Portfolio-Overview section (#019's last open sub-item) is closed, not deferred: the Portfolio Review is a **pure reporting surface** for the assets and their aggregate, and limit information does not belong on it. Limit status stays exclusively on the limits surface at `/back-office#limits` — a deliberate **separation of concerns (reporting vs. compliance monitoring)**. #019's own status is unchanged (`in-progress`), and its remaining follow-ups (edit mode, AUM-forecast importer warning, PDF export, class→investment drill-down) are untouched; only the "beyond B5d" framing of that sentence is dropped, since there is no longer a B5d to be beyond. **(2) #010 Portfolio-Review filter mechanism — won't-do, superseded by #046 and #047.** The *filter interaction model* is rejected: the review will not offer subset selection. It will instead show **everything, hierarchically** (successor **#046**), and the investor-report perspective the filters were "intended to become a core mechanic for" is now explicitly its **own** item (**#047**) rather than an unstated motivation hiding inside a filter feature. The already-existing backend parameter `PortfolioReviewService.get_portfolio_overview(investment_ids=…)` **stays in the service API** — unused by the review UI, available to future callers (#046's per-class and per-investment tile sets are the obvious ones); **removing it is explicitly not part of this decision**. **(3) #046 raised** under **Features** (P2, open) — Portfolio Review restructure: portfolio-level aggregate tile set → aggregate tile set **per sub-asset class** → 6-tile set **per investment**, in a fixed order with no filter or selection interaction; a long "many slides" page by design, and the template/starting basis for #047. Aggregation reuses the **existing converted review seam** (ADR-0099 §4 / ADR-0102) server-side. **(4) #047 raised** under **Features** (P3, open) — Investor Report definition: content and structure (on #046's hierarchy), periodicity, audience, narrative/commentary blocks, branding, output format; **definition first (concept ADR), implementation after**; explicitly low priority, scheduled well after the demo day; expected to converge with **#001**'s PDF output path when both mature. **(5) #022 remainder deferred past demo day.** The open half of Shirley's system-prompt grounding (dataset context + negative hints) is postponed until after the demo; the item stays `in-progress` (the tool-list half shipped 2026-06-02) at unchanged priority, and the coupling recommendation — run the dataset-context half **together with #020** — is unchanged and applies at the deferred date. **Convention added:** `won't-do` is now a documented status under §Status (closed without shipping; the row stays in its bucket rather than moving to **Shipped**, which records what was *built*) — #010 is its first use, and it needed a legend entry. Next free ID → **#048**. |
| 2026-07-13 | **Planning Desk programme raised** (ADR-0103 / ADR-0104, both **Accepted** 2026-07-13; documentation-only — no code shipped against either yet). **ADRs accepted ahead of implementation** on the ADR-0090–0093 design-first precedent, as each ADR's §Operator action requires (accept **and** register before any implementation prompt is produced): `docs/adr/README.md` gained index rows for **0103** and **0104**, the next free ADR number moved **0103 → 0105**, and both ADR bodies were flipped `Proposed` → `Accepted` with a Revision-History row (the ADR-0102 precedent). **#048 raised** under **Loose ends** (P1, open, **Demo: yes**, ADR-0103) — *Cash as first-class asset class*: cash is split today between explicit FX rows (ADR-0100, `reported`) and a functional-currency residual over `portfolio_aum`, so one asset class has two mechanisms and every engine either special-cases it or quietly answers differently depending on the currency it is held in. ADR-0103 unifies them — **all** cash explicit and **unitised** with stored unity prices (`price ≡ 1.0000`, the constraint #038 pinned for exactly this successor), statement-fed via a new workbook **Cash sheet** (**v32**) with ADR-0060 carry-forward between statements, **`flow_type='investor_flow'`** as the eighth canonical member, a **materialised cash plan path**, the efficient-frontier cash exclusion pinned at the assembly seam, and **`portfolio_aum` retired by forward migration** — the last **strictly after** cash materialises correctly and one reconciliation cycle passes (annex §A.3 ordering). Filed under **Loose ends** because it *finishes what ADR-0100 deliberately built only half of* (the "make all cash explicit and demote the residual" option ADR-0100 rejected **for v1** on a daily-maintenance argument that dissolves at statement frequency) rather than adding a capability. Consequence inventory **39 files** (ADR-0103 §7) — the widest of the programme so far, hence one strand and its own implementation chat; migrations claimed at implementation time, not reserved here. **#049 raised** under **Features** (P1, **blocked by #048**, **Demo: yes**, ADR-0104) — *Planning Desk: seventh area + Cash Flow Planning v1*: the **`planning_desk` area** (the **seventh** Area — an architectural addition, and ADR-0104 is the ADR that CLAUDE.md requires for one), two stacked sections, a sticky parameter strip, the Cash Flow Planning lens (per-currency balance timeline over the ADR-0060 seam, pacing rows), hypothetical-transaction entry, and the baseline projection over the plan world. Its load-bearing design is the **overlay contract** (ADR-0104 §2): a scenario is a **parameter set, not a dataset** — an ordered list of exactly four pure `frames → frames` transformations (`insert_transaction`, `repace_flows`, `market_shock`, `fx_shock`) over assembled baseline frames, in a DB-free module the ADR-0013 purity guard extends to; no overlay ever writes `position_transactions`, `investment_navs`, or `instrument_prices`, and engines consume transformed frames **unchanged**, so no engine forks. #049 ships the first two kinds' surfaces; `market_shock` / `fx_shock` surfaces ship with **#034**. Pacing rows are shown **disabled rather than hidden** where no manager plan exists (the visible hook for #023). **Five items updated.** **#023** (Takahashi–Alexander pacing engine): now carries **ADR-D**, *commissioned by* ADR-0104 §2 — so it is written against a fixed contract, not a green field; TA is the **generator** for missing manager plans and the **engine** for #034's richer regimes, and is **never calibrated to reproduce an existing plan** (**D18** — where a plan exists it *is* the baseline). Strand 3, after #049. **#034** (Scenario Analysis): **re-anchored out of the Decision Console** — it becomes the `scenario-analysis` section of the **Planning Desk** (ADR-0104 §6/§8), which settles the last of its four open design questions (how results are surfaced); it now carries **ADR-E** (*commissioned by* ADR-0104: scenario **timing regimes** beyond immediate-t₀, the one thing ADR-0104 deliberately left it), ships `market_shock`/`fx_shock` against the four-kind contract without adding a fifth, and computes **deltas-first over existing engines** (§5). Strand 4, after #049. **#038** (Position model): the **blocked-by note is resolved** — its forward references to unnamed successors now point at **#048** (the former **ADR-A**, cash) and **#049** (the former **ADR-C**, plan-path/overlay); both written, both accepted, and both **honouring the constraints #038 pinned** (cash as `valuation_mode='unitised'` / `price ≡ 1.0000`; plan rows value-based, hypothetical transactions never writing book rows). #038's own state is unchanged. **#032** (BerVersV pre-fill): cross-reference added (**linkage only — resolution stays #032's**) — the **`position_transactions` ledger** is the natural home of lot/holding attribution and therefore of the SV-membership question, which had no finer-grained entity to live on when the item was raised. **#042** (live FX supply): **stays deferred at P3**, but ADR-0104 §3 **raises the operational stakes** — the plan world is built by carry-forward over the same rate frame, so on a multi-currency tenant a missing pair now blocks the Planning Desk **user-visibly** (`MissingFxRateError`, still no silent 1:1 anywhere); the manual burden an ECB SDMX adapter removes is now larger. **Beyond the delta, to prevent a contradiction:** **#033** (Decision Console) gained a note that its **Scenarios** section — an ADR-0089 placeholder anchor, never a surface — is **deleted** by #049 (ADR-0104 §8 amends ADR-0089), leaving the console with three sections (Briefing / Journal / Watchlist) and a crisper identity: it *watches and raises*, the Planning Desk *projects and simulates*. The §Demo-path-flag convention note was corrected, since its "every item is currently `no`" claim is false as of #048/#049 — the **first two `yes` flags**. **`#044` remains unissued** and is not back-filled (ADR-0102 numbering hole, §ID scheme). Next free ID → **#050**. **Operator action:** CLAUDE.md still states **six** Areas and lists six valid `module_area` values — correct today, since `planning_desk` does not exist in the registry; it must be reconciled to **seven** when **#049** lands, not before. |
| 2026-07-15 | **#036 Live Data Import — the `synthetic` provider is now `routing: forced_only`** (implementation-level policy refinement under ADR-0091; **no ADR edit** — ADR-0091 fixes the matrix-routing mechanism but does not mandate that synthetic participate in priority routing, so making it forced-only is recorded in the matrix and here). A new optional per-provider **`routing`** key on the capability matrix (`config/market_data_capabilities.yaml`) takes `normal` (the default when the key is absent) or `forced_only`; the factory's entry validation (`_parse_entry` + `_ALLOWED_PROVIDER_KEYS` / `_ALLOWED_ROUTING_VALUES`) accepts only those two and rejects any other value **at load**, `ProviderCapability` gained a `routing` field + `forced_only` predicate, and `CapabilityMatrix.resolve` (the **unforced** priority path) now skips a `forced_only` provider entirely — while `ProviderCapability.serves` stays **routing-blind**, so the forced `--provider` path (guarded by `live_refresh._forced_capability_serves`, coverage-only) is unchanged. The `synthetic` entry is flagged `forced_only`. **Rationale (verified defect):** synthetic declared full scheme coverage and every ingestable kind at priority 0, so for a `(scheme, kind)` no real provider serves — e.g. `(ticker, coupon)`, or any kind while bloomberg ships disabled — unforced routing had nothing above it and fell through to synthetic, whose adapter build then raised `MarketDataConfigurationError` when `MARKET_DATA_SYNTHETIC_FIXTURE` was unset. Because `live_refresh`'s per-investment `try` wraps the **whole** `_INGESTABLE_KINDS` loop, that errored *every* eligible investment (`errors=1, refreshed=0`) even after its `nav_price` had already ingested. `forced_only` fixes it at the routing seam: the unforced path yields no route for such kinds, the kind loop's existing `None`-route `continue` handles it cleanly, and synthetic's adapter is never built — so the empty-fixture `.env` workaround is no longer needed for a normal tick (`considered≥1, refreshed≥1, errors=0`). The forced `portfoliflow market-data-tick --provider synthetic` path is byte-for-byte unchanged, as are `_INGESTABLE_KINDS`, the per-investment error containment, and the `refreshed`-counting semantics. **Tests:** factory pins — `(ticker, coupon)` / `(isin, nav_price)` are now unroutable on the unforced path (raise `UnsupportedCapabilityError`), a `forced_only` provider is excluded from `resolve` yet its coverage stays routing-blind, an absent `routing` defaults to `normal`, and an invalid value is rejected loudly — plus a `live_refresh` regression: an unforced refresh of a ticker-primary unitised investment with the fixture **unset** completes with `errors=0` and counts as refreshed once `nav_price` (routed to yahoo, network-free fake) ingests. **Loose end flagged (deliberately NOT fixed here, ADR-untouched adjacency):** an investment whose real-provider fetch fails on a *later* kind after earlier kinds ingested still counts `errors=1, refreshed=0` — the whole-loop error containment and the partial-success `refreshed` counting are untouched. Restricted test scope (the `market_data` factory + `live_refresh` suites only, both green); full `pytest` remains an operator action. |
| 2026-07-15 | **#036 Live Data Import — Excel primary-identifier promotion is now type-aware** (implementation-level; ADR-0090 body **unaffected** — §"Identifiers enter through both import paths" never fixed the preference, so the original ISIN-else-ticker rule was only a slice-2 implementation choice recorded in this log on 2026-07-06). `_reconcile_excel_identifiers` Phase 4 (`services/investments/investment_service.py`) promotes a primary type-awarely for any imported investment lacking one: a **market-linked** type (`MARKET_LINKED_TYPES` = `listed_equity` / `listed_bonds`) now prefers its **ticker** (falling back to ISIN), while every other type keeps **ISIN**-first (falling back to ticker); `figi`/`cusip`/`internal` stay non-promotable. **Rationale:** the live-tick path (ADR-0093) addresses providers by the *primary* identifier, and the only wired adapter — Yahoo (`config/market_data_capabilities.yaml` + `services/market_data/adapters/yahoo.py`) — routes **ticker only** (ISIN→ticker via OpenFIGI is a separate seam, not in the tick path), so a promoted primary ISIN left a fully-eligible `listed_equity`/`listed_bonds` un-addressable and forced a manual re-primary per investment on every fresh import. The write loop now carries `imp.investment_type` into the reconcile targets (`identifier_targets: list[tuple[UUID, str, tuple[ImportedIdentifier, ...]]]`) so Phase 4 needs no per-investment re-read. **Invariant preserved:** promotion still fires *only* when the investment has no primary at all (`any(r.is_primary …): continue` untouched), so a manually chosen primary of any source survives every re-import. **Out of scope (unchanged):** `MARKET_USABLE_SCHEMES`, the `is_market_linked` eligibility gate, the capability matrix, and auto-unitise-at-creation (ADR-0097 §6 keeps the flip an explicit operator act); no data migration and no re-promotion of existing rows (structurally a no-op for already-primaried investments). **Tests:** `tests/services/test_investment_service_transform_identifiers.py` — ID-01/ID-02 now expect the **ticker** primary for a `listed_equity` ISIN+ticker fixture, ID-03's comments reconciled (its import-1 primary is now the ticker, dropped in import-2, so the ISIN is re-promoted as the sole market-usable fallback — assertions unchanged), plus **ID-05** (a `private_equity` ISIN+ticker keeps ISIN-first) and **ID-06** (an existing `manual` primary is not re-promoted on re-import). Touched suite + adjacent `test_investment_service_import_v31.py` green (8 passed); full `pytest` is an operator action. |
| 2026-07-17 | **Strand 3+4 closed — the Planning Desk is complete to the ADR-0104/0105 v1 horizon** (documentation-only pass; the strand's code landed `f30c3b7`…`d02d7e2`, 2026-07-14→17). Closure document: `docs/handover/strand-3plus4-adr-0104-0105-closure.md`, in the Strand-1/Strand-2 pattern. **Alembic head b030 unchanged — no migration anywhere in the strand.** Next free ADR **0106**; next free roadmap ID **#050**; E7 (hygiene items recorded ID-less) unchanged. **#034 → v1-scope shipped (2026-07-17), not closed.** The ADR-0104 Scenario Analysis slice is live as the Planning Desk's `scenario_analysis` section: the `market_shock` / `fx_shock` surfaces against the four-kind overlay contract (**no fifth kind**), results assembled **deltas-first over the existing engines** (§5), rendered as a baseline/scenario chart pair on **shared axes** with a ghost baseline, KPI deltas, and limit headroom; the server still holds no scenario state. **Demo-proven** — the live demo on the S34.5 state passed. Two shape decisions are on file: **`fx_shock` stays out of `_EXECUTORS` / `EXECUTABLE_KINDS`** (membership is not executability — it acts at the **conversion seam**, not on `PlanFrames`, so `partition_fx_shocks` routes it and the seam restates a shocked converter; a registered executor would have required putting the FX path *into* the frames), and **`SEAM_COLOUR` stays a shared chart-spec constant** imported from `cash_flow_timeline` and re-exported — the Strand-2 decision 4.13 / loose end 8.5 resolution, taken exactly as that loose end anticipated (`chart_theme.json` carries no semantic-status colours; promoting the amber is a wider theme-contract decision). **ADR-E stays commissioned-open** for the wider vision: timing regimes beyond immediate-t₀ (`market_shock` v1 is **level-shift, immediate-t₀ only**, strictly after the seam), rate/duration, spreads, default/recovery. **#023 → milestone note only; the item stays `open`.** ADR-0105 (the former **ADR-D**, Accepted 2026-07-14 *inside* the strand) discharged its commission for the **TA slice only**: **ephemeral** Takahashi–Alexander generation for **plan-less capital-account funds** at the plan-world seam, activating the pacing rows #049 shipped **disabled rather than hidden**. It **writes nothing** (trap-repository test), and is **never calibrated to reproduce an existing plan** (D18) — by construction, since the generator runs only where the remaining profile is empty. The strand's largest deviation is recorded verbatim in the closure (§4.5): **generated flows settle into `frames.cash_paths`, not only `plan_flows`** — the prompt's "they join `plan_flows`" would have made TA **invisible** (the cash lens renders `cash_paths`) *and* let re-pacing lift a call off a path where it was never set down, so `_settled` applies the **same** ADR-0103 §6 projection through the **same `add_step` primitive** the executors use; **one settlement rule, applied at the seam** rather than by the book-reading materialisation service (ADR-0105 §2's "no new settlement rule"). Its **accepted cost, as a deliberate v1 posture:** because **E4** forbids the offsetting NAV path, **Σ NAV falls for TA funds** — a generated call moves cash down with no NAV position moving up. The **forward limit forecast remains #023's open remainder** and is untouched. **#049 NOT flipped to Shipped — one operator dependency.** The `strand-2-adr-0104-closure.md` **§10 operator gates are still unfilled** (all four: S2.6 report, full-suite result, plan-data gate, demo smoke — each still `______`), as are its §6 rounding table and §3.17 / §4.1 items. A Shipped status not backed by recorded gate evidence would be the closure asserting what no one verified, so **#049 stays `open (#048 shipped)`** and the dependency is flagged here per the closure's §8. **Operator action:** fill the Strand-2 §10 placeholders, then flip #049. **One open documentation-duty item, recorded as an ID-less hygiene loose end (closure §6.1), deliberately not fixed in this pass:** ADR-0105 §15 requires the "moves flows / revalues, but asserts no offsetting NAV consequence" reader-note at every site a reader expects NAV to move. Three of four carry it — `execute_repace_flows`, `_with_ta_profiles`, and `execute_market_shock` as the deliberate **counter**-case (a shock *does* move NAV by design). The **chart assembly** (`services/chart_specs/scenario_impact.py`, and/or the Σ-NAV AUM tile in `services/planning_desk/scenario_results.py`) does **not**: it documents shared axes, ghost, seam, and identical-history but says nothing about Σ NAV *falling* for TA funds and deferred calls — which is the very surface where an operator *sees* the fall. **Docstring-only follow-up**, correctly separated from the closure. **Four deliberate remaining gaps** stand as chosen v1 boundaries (closure §7): richer shock timing (ADR-E), scenario **persistence** (reproducible from *(book, URL)*, not saved), **Shirley/Irene access** (TA is Planning-Desk-only by design — ephemeral, no book row a tool could read), and the **repace-NAV / TA-NAV successor** (one question, not two; reserved for a successor ADR). **S34.0 dropped** — its Telegram-bot test-isolation subject (Strand-2 loose end 8.1) had already landed as `669e370`. |
