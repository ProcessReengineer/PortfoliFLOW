# PortfoliFLOW — Roadmap

**Status:** feature-complete since 2026-09-29 — refinement of the existing Areas;
no new Areas and no wholly new functional areas.
**Last updated:** 2026-11-02
**Owner:** Soenke (ProcessReengineer)

This is the steering document for PortfoliFLOW: what is being worked on, what
comes next and in which order, which ideas are on file, what was decided
against, and what has shipped. It records no history — that lives in git and in
[`CHANGELOG.md`](../CHANGELOG.md). Design and rationale live in the ADRs
([`docs/adr/`](adr/README.md)); on any conflict, **the ADR wins**. Terms follow
[`docs/glossary.md`](glossary.md).

**How to read it.**

- Items carry a flat running number (`#001`, `#002`, …) that never encodes the
  bucket and is never reused. **Next free ID: `#094`.**
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

### #070 — UX overhaul and user manual

- **Purpose:** bring every Area to one consistent, calmer interface — one
  Section per view, intent first, Shirley dockable in every view — and write the
  user manual on top of it.
- **Shipped so far:** the shell, design tokens and component layer (A-0), the
  Transactions Area as the pilot (A-1) and the Watch Desk on M-WD-1 v2 (A-6:
  Briefing, Watch points, Journal; seven families added to the vocabulary).
- **Remaining, in order:** A-2 Back Office, A-3 Cases (as the acceptance walk of
  #078, which builds the Section on the pf vocabulary directly), A-4 Admin and
  My settings, A-5 Front Office and the shell, A-7 Assistants; then the user
  manual.
- **ADR:** ADR-0134 (Section as one view); conventions in
  [`docs/ux/ui-standards.md`](ux/ui-standards.md).
- **Depends on:** none; A-6 shipped 2026-10-10 on #069.

### #076 — Voice providers, round 2

- **Purpose:** Shirley's voice messages (web chat and Telegram) stop depending
  on OpenAI, whose speech models retire on 2027-01-06 (text-to-speech) and
  2027-02-26 (transcription). Speech-to-text and text-to-speech become two
  ports with one adapter each, chosen per tenant and per half: any OpenAI-wire
  server (a tenant's own, keyless), ElevenLabs natively, or OpenAI until its
  shutdown. No provider default — with nothing chosen the half is inactive and
  the Admin card says so; the program always starts. Telegram voice replies
  are transcoded to Ogg/Opus with the host `ffmpeg` when needed.
- **Shipped so far:** the implementation strand (ADR-0136): the two speech
  ports with one adapter per half; provider selection per half on the Voice
  cards with no provider default and a computed state line; the keyless
  `openai_compatible` adapter, the native ElevenLabs adapter and the legacy
  `openai` declaration; Ogg check and `ffmpeg` transcoding for Telegram voice
  notes; boolean settings on the Admin cards as switches; operator
  documentation and the local-engines deployment guide.
- **Remaining:** the implementation walk on a real ElevenLabs key and a local
  engine (deferred to the voice-call strand #077, which needs the same
  credential session); the legacy `openai` declaration is removed after
  2027-02-26. ElevenLabs' `opus_48000_64` was observed Ogg-containered on a
  real account on 2026-10-14 — the adapter declares `audio/ogg` and Telegram
  needs no transcode for it; the observation ADR-0136 §9 left open is closed.
- **ADR:** ADR-0136.
- **Depends on:** none. Must ship before 2027-01-06.

### #077 — Shirley voice call

- **Purpose:** the phone mode the website announces — a fluid spoken
  conversation with Shirley in the browser, interruption included. A
  vendor-hosted conversational agent (ElevenLabs Agents in v1, behind a call
  port) carries Shirley's persona and knows nothing about the book; one
  read-only client tool, `ask_shirley`, runs an ordinary Shirley turn through
  the user's session and relays the answer, and the call's transcript lands
  in the chat history. Per-tenant opt-in with its own key and no
  deployment-wide switch; the vendor agent is provisioned from a definition
  in the repository. Voice messages stay as they are; Telegram keeps voice
  messages only.
- **Shipped so far:** the implementation strand (ADR-0137): the `voice_call`
  declaration and Admin card with its state line; the call package
  (`services/voice_call/`: port, ElevenLabs provider, agent definition with
  the composed call prompt and a generated capability block, read-only tool
  allowlist, speakable rewrite, job records); the `ask_shirley` job behind
  the turn lock with its relay and history lines; the session endpoint with
  lazy, hash-compared agent provisioning; the browser panel and the vendored
  SDK adapter; `/static` revalidation; operator documentation (handbook §8a).
  Walked on a real account 2026-10-15/16 and 2026-10-24: calls, lookups with
  charts, interruption, extending a pending request, timeouts, both languages,
  the failure surfaces, mute, call end and reload; no code defect in the
  record. Release hygiene after the build (key scrub in the voice adapter,
  per-tenant first-start lock, same-name agent adoption, memo drop on a failed
  mint); the Soul's character edit and the first-person call addendum with a
  complete capability list; the card's model and voice lists on request; the
  default conversation model chosen by ear.
- **Remaining:** the conversation style — today the tool call blocks and an
  interruption discards the running lookup — is a separate owner decision
  (non-blocking delivery as sketched under the ADR's Alternatives; spike S-2
  and a successor ADR pending), not part of this item; the vendor's
  per-minute price and the EU-residency question before the first tenant.
- **ADR:** ADR-0137.
- **Depends on:** #076 (shipped). Not blocked by #071 — a call behind another
  user's turn waits visibly; #071 removes the wait.

### #078 — Cases rework: the working file

- **Purpose:** make Cases the place a portfolio manager wants to work in. A
  case becomes a question or task about the book kept open longer than one
  conversation: a structured, editable working file — the question, where we
  stand (read live from the book, the Watch Desk and Transactions), threads,
  figures with provenance, conversations, open points, decision — over the
  existing append-only record, which is written only at milestones and freezes
  the file each time. Waiting as a third state; Keep instead of pin dialogs;
  Shirley briefed by the file, with case tools; linked investments,
  counterparty and tickets on the case; one Section with the case as its
  sub-surface. Documentation becomes a by-product of the work.
- **Shipped so far:** nothing — the concept closed 2026-11-02; the record is
  [`docs/concepts/cases-rework-concept.md`](concepts/cases-rework-concept.md),
  the binding mockup M-CS-2 is kept outside the tree.
- **Remaining:** the ADR-0107 successor, then the build in six strands (model
  and states with migration b037; where we stand and system lines; the
  Section; Keep and the two new kept classes; Shirley tools and proposals;
  close-out). The former UX strand A-3 becomes this item's acceptance walk.
- **ADR:** ADR-0107 successor (to be filed; ADR-0107, 0114 and 0120 stay as
  built).
- **Depends on:** none. Feeds #089 (the case is where background jobs land).

---

## Next

| Rank | ID | Title | ADR |
|---|---|---|---|
| 1 | #072 | Tenant and credential resolution in the bot and the AI core | — |
| 2 | #065 | openai SDK 3.x migration | — |
| 3 | #071 | Per-user / per-tenant AI turn lock | — |
| 4 | #066 | Installer: `--doctor` re-resolves the engine | ADR-0124 §1.1 |
| 5 | #064 | Provider retry policy in the tick | ADR-0091, ADR-0125 |
| 6 | #035 | DataStore decommission | ADR-0094 §5 |
| 7 | #041 | Functional-currency identifier renames (`*_eur`) | ADR-0099, ADR-0101 |
| 8 | #019 | Limit-set editing | ADR-0055–0057 |
| 9 | #046 | Portfolio Review restructure (hierarchical tile sets) | — |
| 10 | #001 | Portfolio Review PDF export and detail areas | ADR-0073 §5, ADR-0020 |
| 11 | #063 | Market-data trading-hours awareness | ADR-0125 |
| 12 | #042 | Live FX-rate supply | ADR-0099 §5, ADR-0091 |
| 13 | #025 | Hosted deployment (Hetzner) | — |
| 14 | #036 | Bloomberg live smoke | ADR-0090 – ADR-0093, ADR-0096 |
| 15 | #007 | Investment sub-class field | — |
| 16 | #031 | Sample-data fidelity follow-ups | ADR-0081 |
| 17 | #073 | Scope the autouse `reset_schema` in `tests/services` | — |
| 18 | #074 | Register default AI tools from the composition root | — |
| 19 | #075 | Speed up the example-portfolio roundtrip test | — |
| 20 | #079 | FX-rate series read tool for Shirley | — |
| 21 | #080 | Manager view | — |
| 22 | #081 | `nav_move` watch family for reported investments | ADR-0116 annex |
| 23 | #082 | Press review per investment or manager | ADR-0022, ADR-0023 |
| 24 | #083 | Document input for Shirley | ADR-0022, ADR-0023 |
| 25 | #084 | Reading room | — |
| 26 | #085 | Report Scraper: highlights and monthly reports | ADR-0053 |
| 27 | #086 | Stated-vs-book performance reconciliation | — |
| 28 | #087 | Prospective-commitment impact | ADR-0104 |
| 29 | #088 | Hedge ratio and currency exposure per fund | — |
| 30 | #089 | Background Shirley jobs landing in a case | — |
| 31 | #090 | Generated briefing documents | ADR-0073 §5 |
| 32 | #091 | Dictated conversation notes | ADR-0136 |

### #072 — Tenant and credential resolution in the bot and the AI core

- **Purpose:** the bot's environment-token dispatcher runs without a tenant
  when `TELEGRAM_BOT_TOKEN` is set and `DATABASE_URL_SUPERUSER` is not; the AI
  core's stored-credential path is reached only from tests.
- **Remaining:** investigate first, then fix or document.
- **Why rank 1:** tenant isolation is the first thing a reviewer checks.

### #065 — openai SDK 3.x migration

- **Purpose:** lift the `openai<3` bound in `pyproject.toml`, which is a freeze,
  not a fix: 3.x moved its HTTP layer to `httpx2`, which the test doubles do not
  intercept.
- **Remaining:** move the LLM and voice clients to 3.x and the test doubles off
  transport-level `httpx` mocking; then remove the bound.

### #071 — Per-user / per-tenant AI turn lock

- **Purpose:** `_TURN_LOCK` (ADR-0031) is still process-wide although
  multi-user has shipped, so turns of different users and tenants serialise.
- **Remaining:** scope the lock per user and per tenant; needs an ADR.
- **Needed by:** #089 (background jobs run under the scoped lock).

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
- **Depends on:** #046. #090 names this item's export path.

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
- **ADR:** ADR-0099 §5, ADR-0091, ADR-0092. **Depends on:** #036. #079 is the
  read-tool sibling.

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

### #073 — Scope the autouse `reset_schema` in `tests/services`

- **Purpose:** scope the autouse `reset_schema` fixture in `tests/services`
  (Q-CL-D-8); its behaviour is documented in `docs/testing.md`.
- **Remaining:** the refactor, deferred from the cleanup.

### #074 — Register default AI tools from the composition root

- **Purpose:** removes the last sanctioned lazy import
  (`_register_default_tools`, Q-CL-D-9).
- **Remaining:** move the default-tool registration to the composition root.

### #075 — Speed up the example-portfolio roundtrip test

- **Purpose:** `test_example_portfolio_roundtrip_lands_investments_and_limits`
  takes ~550 s — DB import, not parsing (Q-CL-D-16).
- **Remaining:** a slow marker or a smaller fixture.

### #079 — FX-rate series read tool for Shirley

- **Purpose:** Shirley cannot read an FX-rate series; `fx_rate` is used only
  inside conversions. "Lay USD/EUR beside this fund" is not answerable.
- **Remaining:** one read tool over `FxRateRepository` returning a dated
  series for a pair, chartable as a second series by `render_chart`.
- **Depends on:** none; #042 is the supply-side sibling.

### #080 — Manager view

- **Purpose:** `manager_name` is free text on the investment and nothing
  aggregates by it; "how have this GP's funds developed?" is a per-fund
  exercise today.
- **Remaining:** a manager-level read over the book — funds, vintages,
  commitments, NAV, exposure, multiples — as a Shirley tool first; a Front
  Office surface later. Decide whether `manager_name` is normalised or matched
  as is.

### #081 — `nav_move` watch family for reported investments

- **Purpose:** a sharp NAV move on a reported (non-priced) investment is not a
  finding; the `price` family watches instrument prices only.
- **Remaining:** a `nav_move` family over `InvestmentNav` in badness units like
  `price`, with its own watchpoint shape, Approaching/Triggered rendering and
  briefing mention.
- **ADR:** an ADR-0116 annex.

### #082 — Press review per investment or manager

- **Purpose:** the web research tool searches the curated feeds over a short
  window for a free query; a press review on one fund or manager over a longer
  period is not a first-class ask.
- **Remaining:** scope the query to an investment or manager, allow a longer
  window, return the review in a shape a case can keep.
- **ADR:** ADR-0022, ADR-0023 (Fetcher discipline unchanged).

### #083 — Document input for Shirley

- **Purpose:** the chat composer accepts images only; a PDF (a GP special
  report, a PPM page) cannot be handed to Shirley.
- **Remaining:** document attachments in the chat with the Fetcher discipline
  for untrusted documents (summary wrapped as external content, never the raw
  text in the agent's context), excerpt by page, size and type caps.
- **ADR:** ADR-0022, ADR-0023. Takes up the document-reading half of #003.

### #084 — Reading room

- **Purpose:** due diligence means many documents; a case can hold attachments
  but nothing reads them.
- **Remaining:** a bounded, case-scoped document set Shirley reads on request
  and answers questions over; results land in the case as excerpts and
  figures. No DMS semantics — no folders, versions or content-search surface;
  large sets go through an extraction pipeline (the Report Scraper is that
  pipeline for one document type).
- **Depends on:** #083, #078.

### #085 — Report Scraper: highlights and monthly reports

- **Purpose:** the Scraper extracts keyword values with sources from one
  quarterly PDF; it keeps no narrative highlights and is not tuned to monthly
  letters.
- **Remaining:** a highlights/summary output beside the values; monthly
  cadence; a per-fund "last two months" read for #090.
- **ADR:** ADR-0053.

### #086 — Stated-vs-book performance reconciliation

- **Purpose:** the book computes rolling TVPI/DPI/RVPI and IRR per investment;
  laying a GP's stated figures beside ours as of one date, and naming the
  usual reasons for a gap, is manual.
- **Remaining:** a comparison helper over the existing series plus typed
  stated figures (gross/net, as-of, FX, subscription line, fee timing as the
  named candidates); a Shirley tool and a Figures-row shape for #078.

### #087 — Prospective-commitment impact

- **Purpose:** how a new fund would change SAA, AnlV headroom and liquidity is
  the question behind every re-up; the impact panel covers order tickets only
  and `get_saa_hypothetical_comparison` covers SAA only. The U-NEW impact was
  the named fast-follow of the Transactions track and never became an item.
- **Remaining:** U-NEW impact in the impact panel; a hypothetical commitment in
  the plan world; the three lenses as for order tickets.
- **ADR:** ADR-0104; the impact-panel decisions of the Transactions track.

### #088 — Hedge ratio and currency exposure per fund

- **Purpose:** a fund's reported hedge ratio and currency exposure are not data
  points; the gap between USD and EUR performance of a hedged fund cannot be
  attributed.
- **Remaining:** hedge ratio per fund and period as reported data (candidate:
  `type_specific_data`), shown on the investment and readable by Shirley.
  Ties into #045.

### #089 — Background Shirley jobs landing in a case

- **Purpose:** a multi-step ask ("put together everything I need for the AGM")
  runs longer than a chat turn; every turn blocks behind the turn lock and
  results live only in the ephemeral chat history.
- **Remaining:** a job runner (the `ask_shirley` job of ADR-0137 is the seed)
  with the case as the inbox — progress line, result as a proposal package
  accepted item by item, system lines on request and landing.
- **Depends on:** #071 (the lock), #078 (the landing place).

### #090 — Generated briefing documents

- **Purpose:** Shirley's output is chat prose; a composed memo — per fund:
  highlights, figures, charts, press — as an editable, exportable artefact does
  not exist.
- **Remaining:** a generated-document artefact kept in a case, PM-editable
  after acceptance, with its sources; export to PDF over the #001 path.
- **ADR:** ADR-0073 §5 for the export path. **Depends on:** #078, #085, #089.

### #091 — Dictated conversation notes

- **Purpose:** notes after a call are typed; the voice chain already
  transcribes.
- **Remaining:** dictate into a case's Conversations through the
  speech-to-text port; the transcript lands as a note draft the PM edits.
- **ADR:** ADR-0136. **Depends on:** #078.

---

## Ideas

Unprioritised. One paragraph each; none is scheduled.

**#003 — Due-diligence support.** A sibling of the Report Scraper on the same
extraction backend for GP due-diligence documents (PPMs, LPA drafts): fund size,
strategy, track record, key-person clauses. #083 and #084 take up the
document-reading half; the DD-specific extraction (fund size, strategy, track
record, key-person clauses) stays here.

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

**Workflow linting.** Lint the GitHub Actions workflows with `actionlint` and
`zizmor`; needs tool installs.

**Fail fast on an unreachable test container.** Stop the run at the first
connection with "start the test container" instead of letting each module skip
or error (ADR-0135 report, F1).

**Underwater tile successor.** Replace the listed-equity "Underwater" drawdown
tile (Tile 2 of the triplet, ADR-0082) with a chart carrying more meaningful
information while keeping its basic optics (filled area against the zero
line); inspiration from Bloomberg-style terminals. Deferred from the
pre-release polish of `2026.10.0`; needs a short concept note before a build
prompt.


**#092 — Fee terms and fee drag.** Fee terms on the investment (management fee
basis and rate, carry, hurdle, catch-up) and a fee-drag comparison across
funds of one manager or vintage; the question behind every re-up that the
book cannot answer today.

**#093 — Look-through from GP reports.** Portfolio-company level extraction in
the Report Scraper, so "the companies did well but the fund did not" can be
checked against data instead of memory.

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
| #069 | Watch Desk refinement: briefing artefact, watch-point scope, presets | 2026-10-12 | ADR-0133 |

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
