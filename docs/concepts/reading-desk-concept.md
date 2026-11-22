# Reading Desk — concept record

- **Status:** concept closed 2026-11-21 (owner + concept chat; owner points OP-RD-1…4 decided, §11); **amended the same day by the kickoff concept chat** after verification against the image of 2026-11-21 (version `2026.11.0`): RD-D-3 and RD-D-10 revised, RD-D-19…30 added, the §11 points for the ADR/MC resolved. Feeds ADR-0139 (successor to the ADR-0071 stub, amending ADR-0053) and the Mission Control kickoff `KO-RD-MC`
- **Owner:** Soenke · **Written by:** the Report Scraper concept chat, 2026-11-21 (on the brainstorming of 2026-11-09)
- **Name:** *Reading Desk* — the user-facing name of the Section that today is "Report Scraper" (OP-RD-1, decided 2026-11-21)
- **Location:** `docs/concepts/reading-desk-concept.md` in the repository (placed by the operator; no date in the file name). Track artefacts (kickoffs, board, handovers, closing reports, mockups) stay outside the tree under `~/Code/PortfoliFLOW/development/other/reading-desk/`; Claude Code prompts and run reports under `~/Code/PortfoliFLOW/development/prompts/` and `…/development/reports/`.

---

## 1. Why

The Report Scraper (ADR-0027, web surface ADR-0053, per-tenant model ADR-0123) works: it reads a GP document and returns keyword values with source page and confidence. But it has **no memory and no connection to the rest of the system** — by ADR-0053 the operator takes the results away "out-of-band" and they are gone; ADR-0071 reserved a persistence store and never filled it. Five roadmap items circle the same gap (#003, #021, #083, #084, #085). The result is a tool that answers once and forgets: the PM cannot ask six months later what the last quarterly report of a fund said, a deputy cannot see what was read, and Shirley knows nothing of it.

The fix is deliberately narrow. It is **not** due diligence and **not** a new data source for the book (both are named follow-ups, §12). It is: *what PortfoliFLOW has read, it remembers — structured, with a page reference, deletable.*

## 2. Definition

> A **reading** is PortfoliFLOW's structured record of one document about a fund we hold or have a direct relation with (successor fund, a fund we follow). It is written once when the document is read, kept without the document itself, and retrievable by the PM and by Shirley until it is removed.

The PDF passes through; the reading stays. About 90 % of the documents concerned are on funds in the portfolio or directly related to it.

## 3. The two tests a reading must pass

1. **Is asking faster than searching?** "What did the last report of Fund X say about the exit pipeline?" or "what was the NAV in Q2?" is answered from the Reading Desk or by Shirley in less time than finding the PDF in the file share or the mailbox — with the page to check it against.
2. **Can a deputy see what was read?** For every fund: which documents were read, when, by whom, of which type, with what result — in one place, in the same shape.

Mirrors the two tests of the Cases record (ADR-0138) on purpose: the Reading Desk is the institution's reading memory as Cases is its deliberation memory.

## 4. Scope

### 4.1 In v1

- Every reading run persists one reading per document (today's run view stays, its result is no longer lost).
- **Profiles per document type** with typed fields (§5.2); the model classifies the document in the same call; the keyword editor stays as an override for ad-hoc questions.
- A **reading note** from the same call: highlights, quotes with page, events and risks, outlook.
- **Assignment** to an investment, else to a manager (§5.3), proposed by the reading, confirmed by the PM.
- **Library** in the Section: list with filters and text search over readings, reading detail, re-read (requires a new upload), remove with reason.
- **Visibility:** every user of a tenant sees all of the tenant's readings (read-by recorded); users of other tenants see nothing (RLS).
- **Retention:** an explicit "remove all readings older than X years" with preview and one recorded purge per run — tenant owners only.
- **Investment detail page:** a chronological "Documents read" list for the investment.
- **Shirley:** three read tools (list, get, search), registered as untrusted external reads (RD-D-10).
- **Keep into a case:** a quote or a finding of a reading can be kept into a case's working file (§6.3).
- **Door 2 — "Read" on a case attachment** as the **last strand of v1**, droppable if the budget is spent: the bytes are already in `case_attachments`, the reading is written like any other and assigned to the case's linked investment; the natural seed of the reading room (#084).

### 4.2 Not in v1 — named follow-ups

| Follow-up | Why not now |
|---|---|
| Reading Desk as a data source for the book (NAV/cashflow takeover with PM confirmation) | The definite goal of the second step. Establishing a fourth source beside Excel import, Yahoo Finance and Bloomberg needs its own rules: provenance (`ingest_origin`, ADR-0092 annex), conflicts with existing values, precedence. In v1 the PM reads the reading and changes the book by hand. |
| Text layer (local extraction, stored or used for quote verification) | Not stored in v1. Added only if testing shows quotes are unreliable; transient verification without storage is the first option then. |
| Profiles LP letter / special report, monthly letter (#085), AGM papers | Profiles extend by need; the LP letter is the first candidate because it leads into Cases (action requested + deadline). |
| Door 2, if dropped from v1 | Falls back to the R-1 package together with the reading room (#084). |
| Door 3: a PDF in the chat composer (#083) | R-1 package; Shirley would see the reading note, never the raw text (Fetcher discipline). |
| Embeddings / semantic search, `tsvector` full-text | The example questions are structured filters plus a substring search (the Cases idiom); added only if that proves too blunt. |

### 4.3 Non-goals

- **Not a DMS.** No PDFs kept, no folders, no versions. The only documents PortfoliFLOW keeps remain case attachments (ADR-0107 §7 / ADR-0138 boundary unchanged).
- **Not due diligence.** No pipeline or deal object, no data-room workflow, no fit analysis. A real DD Support gets its own concept (§12).
- **Not a source of book data.** Nothing a reading contains reaches `investment_navs`, `investment_cashflows` or any other book table in v1.
- **No analytics.** Comparisons ("average target return of the last three") are Shirley arithmetic over listed findings, no new analytics service.

## 5. Structure

### 5.1 The reading

One row per document read, per tenant (RLS).

| Group | Holds |
|---|---|
| **Identity** | per-tenant running number (shown `R-nnnn`), filename, `sha256`, size, page count *as read by the model* (nullable — no PDF parser is a dependency), read at, read by, origin (upload · case attachment) |
| **Classification** | document type (profile id + profile version), language |
| **Subject** | fund name and manager name as read; assignment: investment (nullable) or manager name (§5.3); document date / reporting date; currency |
| **Findings** | per field: key, value, type, unit, page, confidence, short source quote (JSONB) |
| **Reading note** | highlights (3–7), quotes `{text, page}`, events and risks, outlook (JSONB) |
| **Provenance** | model, prompt version, profile version, ad-hoc keywords used, trust marker |
| **Search** | `search_text` — plain text the writer composes from the head, the findings and the note; searched by case-insensitive substring match |
| **Removal** | `removed_at`, `removed_by`, reason, purge run id (nullable) — tombstone (§5.4) |

Size: a reading is roughly 10–50 KB. A tenant can hold thousands without noticing.

### 5.2 Profiles

Profiles are files in the repository (proposed `config/scraper_profiles/`, like the prompt), versioned; a reading records the profile version it was read with. Field types follow today's keyword types (Number, Percentage, Date, Text, List).

**Common head (every type):** document type · fund name · manager · document/reporting date · currency · language. **Always:** the reading note.

**Quarterly report**
- Key figures: NAV, called (cumulative), distributed (cumulative), unfunded, TVPI, DPI, RVPI, net IRR.
- **LP level vs. fund level are separate fields.** Only the LP level (capital account) is the investor's figure; a separate capital account statement is its own document.
- Portfolio: number of portfolio companies, new investments and exits in the quarter, material valuation changes, team or key-person changes.

**Capital call / distribution notice**
- Kind (call · distribution · combined), value date, amount (LP share), currency.
- Breakdown — call: investment · management fee · partnership expenses; distribution: return of capital · gains · income, recallable portion.
- Called and unfunded after the event.
- **Excluded by design: bank details.** Not extracted, not stored, stripped if returned. Changed bank details in a capital call are a known fraud pattern; an extracted IBAN would be a dangerous convenience.

**Pitch deck / PPM** (assigned to a manager, §5.3)
- Fund: strategy, asset class, region, vintage, target size, hard cap.
- Target return **with its basis** (gross/net, IRR/multiple) — without the basis an average across decks is meaningless.
- Fundraising: status, first close and final close (planned/done), minimum commitment.
- Terms: management fee, carry, hurdle, GP commitment, term and extensions (raw data for #092, no model there yet).
- Team and history: key persons, track record of predecessor funds (fund, vintage, TVPI, IRR).

**Other** — common head and reading note only.

### 5.3 Assignment

- If the fund is held: the reading is assigned to the **investment** (suggested from `investments.name`, confirmed by the PM).
- Otherwise: to the **manager** — a successor fund, a fund being followed — as a name suggested from the known `manager_name` values. No manager object is built (the manager view is #080, which gets "all readings of manager X" for free).
- Unassigned readings are allowed and listed as such.

### 5.4 Lifecycle

- **Read** → reading written at the run's completion path. Dedup per `(tenant, sha256)`: the same PDF again shows "already read on …" with an explicit *Read again* (a new reading, the old one stays until removed).
- **Remove one** → tombstone: identity, removal fields and reason stay; findings, note and `search_text` are purged. `document_readings` carries the standard audit trigger (installed on purpose — the Cases tables carry none), so the removal is in `audit_log`. Tombstones are kept indefinitely.
- **Retention run** (tenant owners only) → "remove all readings with a document date older than X years" (read date where no document date): preview with the count, confirmation, one row in `reading_purges` carrying criterion, cutoff, count and actor; the affected readings become tombstones that reference the purge.
- **Not affected by removal:** anything kept into a case (copied, §6.3) and anything the PM typed into the book by hand.

## 6. Surfaces

### 6.1 Reading Desk (Section under Assistants, beside Shirley)

- **Read** — upload one or several PDFs, profile *auto* (optional preselect), optional ad-hoc keywords; the run view as today (SSE progress, one result block per file — there is no comparison matrix in the tree), each block now naming the reading it was saved as and proposing its assignment.
- **Library** — list with filters (type, fund, manager, investment, period, assigned/unassigned) and text search; reading detail (head, findings with page and confidence, note); assign; remove with reason; *Read again*.
- **Retention** — the retention run (§5.4), with its own confirmation; rendered for tenant owners only, refused server-side for everyone else.

The Section stays under Assistants for v1 — Shirley reads, Shirley remembers. When the book takeover comes in the second step, its gesture sits on the reading and on the investment page; the Section need not move.

### 6.2 Investment detail (Front Office)

A chronological "Documents read" list: date, type, filename, two or three key findings, link to the reading. The first time the scraper's work is visible where the PM works.

### 6.3 Cases

A reading has no PDF, so the existing `document_excerpt` class (attachment + passage + page) cannot carry it. Proposed: **Keep → from the reading detail** copies into the active case — a quote as a new kept class *reading excerpt* (quote, page, filename, document date, reading id as a reference only), a finding as a **Figures** row with source chip `document`. Copy semantics: removing the reading never breaks a case or a frozen snapshot. Adding a kept class is an ADR-0138 annex.

## 7. Shirley's role

- **Three read tools** on the `case_tools.py` pattern: `list_readings` (filters as the library), `get_reading` (head, findings, note), `search_readings` (substring search). All three register as **`READ_EXTERNAL_UNTRUSTED` with `wraps_result_as_untrusted=True`**: the registry wraps the payload as `<external_content trust="untrusted">` — a reading carries GP text (ADR-0071 Q1, ADR-0022). The first draft's "`READ_INTERNAL` with a rewrap" cannot be registered: the registry refuses the wrapping flag on any other class, and the turn lock hangs on the class, not on the wrapping. Tool descriptions only; the Soul stays byte-identical. The stale `analysis_results.scraper.findings` paragraph of `prompts/Shirley_AnalysisResults_Context.md` (nothing on the web writes that dataset) is replaced by a pointer to the readings — owner-released prompt text.
- **Consequence to accept knowingly:** once a reading tool ran in a turn — the list included — `case_add` is locked for the rest of that turn (ADR-0022). "Read the report and put the 80 % into the figures" does not work in one go; the path is a proposal or the PM's *Keep*.
- **Voice call:** the call allowlist tripwire is equality (len 20 since D-CS-M-17); three new tools make it 23 and need a capability line in the call prompt — prompt text under rule 19, released by the owner from the run report before commit.
- Comparisons are Shirley's arithmetic over listed findings; she states which readings she used.

## 8. Boundaries restated

- **Search over readings, never over documents.** The substring search runs over PortfoliFLOW's own structured capture (note, findings, quotes). It is not a content search over attachments — the ADR-0138 §11 rule (no content-search UI over attachments; captions not searched) stays untouched.
- **Quotes are the model's reading.** Without a text layer they are unverified; the UI marks them as such ("as read, p. 12") and shows confidence.
- **Prompt injection is real.** Instructions inside a document are ignored by the prompt; the payload is untrusted everywhere downstream; nothing a reading says acts on its own.
- **The pure scraper package stays pure.** The writer sits in the route layer and is called when the extraction service returns (RD-D-23); `services/scraper/` gains no DB access (ADR-0071 Q5).

## 9. Delta against today

| Component | Today | Target | Change |
|---|---|---|---|
| Scraper service, capability map, one-shot extraction, per-tenant model resolution | built (ADR-0027/0053/0123) | unchanged | keep |
| Keyword editor per run | the only way to ask | override beside profiles | keep, demote |
| Profiles per document type, classification in the call | — | files in `config/scraper_profiles/`, versioned | new |
| Prompt output | findings | findings + reading note, bank details excluded | extend |
| Persistence | none (out-of-band, ADR-0053) | `document_readings` with tombstones and `reading_purges`, migration **b038** (head b037; b035 stays reserved for `engagements`) | new |
| Dedup, re-read | — | per `(tenant, sha256)` | new |
| Library, reading detail, remove, retention | — | in the Section | new |
| Assignment | — | investment or manager name | new |
| Investment detail | — | "Documents read" | new, small |
| Shirley | knows nothing of readings (a stale context paragraph promises a dataset nothing writes) | three read tools as untrusted external reads; call allowlist 20 → 23 | new |
| Cases | — | Keep a quote (new kept class) or a finding (Figures, source `document`) | extend (ADR-0138 annex) |
| Case attachment | stored, not readable by the scraper | "Read" writes a reading (door 2, last strand, droppable) | new |
| ADR-0071 | stub | replaced by the Reading Desk ADR | supersede |
| Section name | "Report Scraper" (slug `report-scraper`) | "Reading Desk" (slug `reading-desk`); visible copy and the Admin label only — `services/scraper/`, the `/scraper/…` run routes, the setting `openrouter.scraper_model` and `SCRAPER_MODEL` keep their names | rename (RD-D-25) |

## 10. Decisions of record proposed for the ADR (RD-D-n)

- **RD-D-1** Definition (§2) and the two tests (§3) are the ADR's context; scope narrow — documents on held or directly related funds.
- **RD-D-2** The PDF passes through; the reading is kept; no time-boxed PDF retention.
- **RD-D-3** *(revised 2026-11-21)* Postgres, per tenant under RLS; JSONB for findings and note; a writer-composed `search_text` searched by case-insensitive substring match — the idiom of the Cases search, which finds "Exit" in "Exitpipeline" where an unstemmed `tsvector` would not; no `tsvector`, no vector store, no embeddings in v1.
- **RD-D-4** Profiles per document type as versioned repository files; v1 = quarterly report, capital call/distribution notice, pitch deck/PPM, other; the model classifies; the keyword editor remains an override.
- **RD-D-5** Bank details are never extracted or stored.
- **RD-D-6** The reading note is produced in the same call; quotes carry a page and are marked as the model's reading.
- **RD-D-7** Assignment: investment if held, else manager name; no manager object.
- **RD-D-8** Removal by tombstone; retention run by document date with preview and one audit event; removal never touches cases or the book.
- **RD-D-9** Dedup per `(tenant, sha256)`; *Read again* writes a new reading.
- **RD-D-10** *(revised 2026-11-21)* Shirley: three read tools, all `READ_EXTERNAL_UNTRUSTED` with the registry's untrusted wrapping; the `case_add` turn lock — triggered by the list as well — is accepted; Soul byte-identical.
- **RD-D-11** Keep into a case copies (quote → reading excerpt, finding → Figures row with source `document`).
- **RD-D-12** No book writes in v1; the book takeover is a separate, later decision.
- **RD-D-13** No text layer in v1.
- **RD-D-14** Section under Assistants, named Reading Desk; readings mirrored on the investment detail page.
- **RD-D-15** Search over readings is not content search over documents; the ADR-0138 DMS boundary holds.
- **RD-D-16** Visibility tenant-wide: every user of a tenant sees all of the tenant's readings, read-by recorded; no reading is visible across tenants (RLS).
- **RD-D-17** The retention run is an admin action; single removal with reason stays open to every user of the tenant.
- **RD-D-18** Door 2 ("Read" on a case attachment) is the last strand of v1 and droppable; if dropped, it moves to R-1 with #084.

Added by the kickoff concept chat, 2026-11-21 (owner-confirmed where marked):

- **RD-D-19** "Admin" is the tenant role `owner` (`require_role("owner")`); no new role model. Reading the library and a reading: every signed-in user of the tenant, `auditor` included. Starting a run, assigning, removing one reading: `owner` and `member`. Retention run: `owner`.
- **RD-D-20** A retention run is recorded as one row in `reading_purges` (criterion, cutoff, count, actor); `document_readings` and `reading_purges` carry the standard audit trigger.
- **RD-D-21** Tombstones are kept indefinitely.
- **RD-D-22** Page count is what the model reports, nullable; v1 adds no PDF parser dependency.
- **RD-D-23** The writer runs when the extraction service returns, independent of the client connection: a closed tab does not lose a paid reading. Without a database engine the run view works as today and nothing is written.
- **RD-D-24** Bank details are excluded by the prompt and removed again by a post-filter inside the pure scraper package, before anything is rendered or stored.
- **RD-D-25** *(owner-confirmed)* The rename is user-visible only: Section title, slug `reading-desk`, UI copy, the Admin label "Reading Desk model". Code identifiers, routes, the settings key and the environment variable keep their names. In the glossary the *Report Scraper* remains the name of the extraction backend.
- **RD-D-26** A reading's document type is not editable; a wrong classification is corrected by a new upload with the profile preselected.
- **RD-D-27** The kept class for a quote is `reading_excerpt`, payload by value, written only by the PM's Keep; it gets its own annex ADR to ADR-0138 (expected ADR-0140).
- **RD-D-28** The extraction call gets an explicit timeout and output budget; the defaults of today (120 s, no `max_tokens`) are not relied on.
- **RD-D-29** Assignment survives an investment's deletion as *unassigned* (`ON DELETE SET NULL`); the fund name as read stays.
- **RD-D-30** Door 2 proposes the case's linked investment when the case links exactly one; otherwise the PM chooses, and unassigned is allowed.

## 11. Open points

**Owner points — decided 2026-11-21**

- **OP-RD-1** Name: **Reading Desk** → RD-D-14.
- **OP-RD-2** Visibility: **all users of a tenant, never other tenants** → RD-D-16.
- **OP-RD-3** Retention run: **admins only** → RD-D-17 (admin = the tenant role `owner`, RD-D-19).
- **OP-RD-4** Door 2: **last strand of v1** (droppable) → RD-D-18.

**For the ADR / MC (not for the owner)**

All resolved 2026-11-21 against the image (version `2026.11.0`):

- "Admin" → the tenant role `owner` → RD-D-19.
- Door 2 assignment → RD-D-30.
- Tombstones → kept indefinitely, RD-D-21.
- Retention run event → `reading_purges`, RD-D-20.
- Wrong classification → new upload with preselect, RD-D-26.
- The reading-excerpt kept class → `reading_excerpt`, annex ADR, RD-D-27; no migration (the kept-class and figure-source vocabularies are repository frozensets, and the figure source `document` exists).
- Output length → RD-D-28; verified live in RD-1 with a long report.
- Investment deleted → RD-D-29.
- Comparison matrix → does not exist in the tree; the run view is one block per file (§6.1).
- IBAN exclusion → RD-D-24.

## 12. Roadmap consequences (for a docs prompt)

| Item | Change |
|---|---|
| Reading Desk v1 | new item **#095**, Active (next free ID verified as #095) |
| #085 Report Scraper: highlights and monthly reports | highlights absorbed by the reading note (#095); re-titled to the monthly-letter profile follow-up |
| #021 Persistent analysis results | the scraper half is delivered by #095 (ADR-0139 supersedes the ADR-0071 stub); the note says so |
| #003 DD Support | re-scoped: **a real DD Support with its own concept and scope still to be worked out**; the P-CS-D1 note "absorbed in part by #083/#084" is revised |
| Reading Desk as a data source for the book | new item **#096**, Next: NAV/cashflow takeover with PM confirmation; provenance (`ingest_origin`), conflicts, precedence beside Excel import, Yahoo Finance and Bloomberg |
| Text layer / quote verification | new item **#097**, Ideas: only if testing shows the need |
| Further profiles | LP letter / special report (first, Cases tie-in), monthly letter (#085), AGM papers |
| #083 document input, #084 reading room | unchanged in R-1; the Reading Desk becomes their pipeline (door 2 already in v1 unless dropped, door 3 = #083) |
| #080 manager view, #092 fee terms, #093 look-through | unchanged; noted as consumers of readings |

Next free roadmap ID in the image of 2026-11-21 is #095; after the docs prompt it is #098.

## 13. Dosing sketch (for the kickoff, not binding)

Within the owner's budget of at most 2–3 intensive days; ×1.6 calibration for a build strand.

- **RD-1 Foundation** (DC-RD-1) — migration b038 with tombstones and purges, profiles and reading note in the extraction contract (bank exclusion), dedup, writer when the service returns. Plan 3 prompts, ceiling 5.
- **RD-2 Surface** (DC-RD-2) — mockup M-RD-1 as a self-contained HTML file in the DC-UX-D style (pause point), then rename, library and reading detail, assignment / remove / retention, investment detail list. Plan 4, ceiling 6.
- **RD-3 Shirley and Cases** (DC-RD-3) — three read tools as untrusted reads, call allowlist and capability line (owner-released prompt text), Keep into a case (annex ADR-0140). Plan 2, ceiling 4.
- **RD-4 Door 2** (DC-RD-3) — "Read" on a case attachment; the last strand, dropped to R-1 if the budget is spent. Plan 1, ceiling 1.

Verified plan: 10 build prompts and 3 docs prompts (ADR-0139 + roadmap; ADR-0140; close-out), one MC chat (`KO-RD-MC`) and three daughter chats (`KO-RD-1`, `KO-RD-2`, `KO-RD-3`).

## 14. References

ADR-0027, 0053, 0123 (Report Scraper as built); ADR-0071 (persistence stub, Q1 trust, Q5 purity); ADR-0022/0023 (trust classes, Fetcher discipline, `case_add` turn lock); ADR-0107/0138 (Cases, DMS boundary, kept classes); ADR-0092 (`ingest_origin`, for the follow-up); D-CS-M-17 (voice allowlist). Brainstorming 2026-11-09, recap 2026-11-20, scope decisions 2026-11-21. Cases Round 2 package R-1 (#083, #084, #085) in `cases-track/Cases-rework_implementation-plan_2026-11-02.md`.
