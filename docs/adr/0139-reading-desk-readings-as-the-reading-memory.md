# ADR-0139: Reading Desk — Readings as the Institution's Reading Memory

- **Status:** Accepted (2026-11-21)
- **Date:** 2026-11-21
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #095 (Reading Desk v1)
- **Supersedes:** ADR-0071 (Persistent Analysis-Results Store — a stub that was never expanded; its six open questions are answered in §12).
- **Amends:** ADR-0053 ("No persistence", "the operator captures the output out-of-band"; the Section's name and slug). ADR-0053, ADR-0027 and ADR-0123 stay Accepted and unedited; this ADR records the revisions (ADR-0134 §3 precedence).
- **Honours:** ADR-0022 (tool trust classes — applied, not changed, §8), ADR-0023 (Fetcher discipline), ADR-0035/0036 (RLS, audit trigger and the `app.user_id` GUC), ADR-0063 (tenant roles), ADR-0107 §7 / ADR-0138 §11 (the DMS boundary), ADR-0123 (per-run, per-tenant model resolution — unchanged), ADR-0134 §4 (Section as one view), ADR-0137 (voice-call tool allowlist), ADR-0008 (English-only codebase).
- **Concept record:** `docs/concepts/reading-desk-concept.md` (closed and amended 2026-11-21; decisions RD-D-1…30 recorded there and carried here).
- **Binding mockup:** `M-RD-1_reading-desk-mockup.html` (kept outside the tree, written in strand RD-2; where mockup and this ADR disagree, this ADR wins, then the code).
- **Tags:** scraper, reading-desk, persistence, schema, shirley, tools, trust, cases, multi-tenant, audit, ui, bait-vait

---

## Context

### The diagnosis

The Report Scraper (ADR-0027; web surface ADR-0053; per-tenant model ADR-0123) reads a GP document and returns keyword values with a source reference and a confidence. It has no memory and no connection to the rest of the system: the result lives in a per-app in-memory store, bounded and dropped on logout, and ADR-0053 leaves it to the operator to carry it away. ADR-0071 reserved a store for run-bound results in June 2026 and stayed a stub. Five roadmap items circle the gap (#003, #021, #083, #084, #085).

The consequence is a tool that answers once and forgets. The PM cannot ask six months later what the last quarterly report of a fund said; a deputy cannot see what was read; Shirley knows nothing of it.

### The definition and the two tests (RD-D-1)

> A **reading** is PortfoliFLOW's structured record of one document about a fund we hold or have a direct relation with (successor fund, a fund we follow). It is written once when the document is read, kept without the document itself, and retrievable by the PM and by Shirley until it is removed.

A reading must pass two tests:

1. **Asking is faster than searching.** "What did the last report of Fund X say about the exit pipeline?" is answered from the Reading Desk or by Shirley faster than the PDF is found in a file share — with the page to check it against.
2. **A deputy can see what was read.** For every fund: which documents were read, when, by whom, of which type, with what result — in one place, in one shape.

The Reading Desk is the institution's reading memory as Cases (ADR-0138) is its deliberation memory.

### Constraints verified against the tree (image of 2026-11-21, version `2026.11.0`)

- `services/scraper/` is DB-free, FastAPI-free and synchronous; `web/routes/scraper.py` drives it through `asyncio.to_thread` inside `_drive_run` / `_run_in_thread` and renders one result block per file (`_partials/scraper_results.html`). There is no comparison matrix.
- `AIServiceCore.send_one_shot_extraction` sends no `max_tokens` and defaults to a 120 s timeout; the service passes neither.
- The prompt is `prompts/Scraper_Prompt.md` (first bare fence), with the output `{fund_name, period, findings{keyword → value, source, confidence}}`.
- Migration head is `b037_add_case_working_file`; `b035` is reserved for `engagements` (ADR-0129). The next free revision is **b038**.
- Tenant roles are `owner` · `member` · `auditor` (`core/models/user.py`); gates are `web.permissions.require_role`; the run routes are open to `owner` and `member`; owner-only Sections use `SectionMeta(owner_only=True)` and `is_tenant_owner`.
- `ToolRegistry.register_tool` refuses `wraps_result_as_untrusted=True` on any class other than `READ_EXTERNAL_UNTRUSTED`; `execute_tool` locks `WRITE_INTERNAL` and `EXTERNAL_EFFECT` for the rest of the turn once a tool **of that class** ran. The lock hangs on the class, not on the wrapping.
- The registry holds 20 tools; `services/voice_call/allowlist.py` pins equality with it, `tests/services/voice_call/test_allowlist.py` and `test_voice_call_layering.py` pin the count 20, and `tests/assistants/test_prompt_composition.py` pins the call prompt's hash.
- The Cases search (`CaseRepository.search_working_file`) is a case-insensitive substring match; no `tsvector` exists in the tree.
- The kept-class and figure-source vocabularies are repository frozensets (`_KEPT_ARTIFACTS`, `_FIGURE_SOURCES` in `case_working_file_repository.py`), not SQL constraints; the figure source `document` exists.
- `case_attachments.content` holds the bytes of an attached document (cap 10 MB by default).
- No PDF parser is among the runtime dependencies.
- `prompts/Shirley_AnalysisResults_Context.md` describes a dataset `analysis_results.scraper.findings` that nothing on the web writes.

---

## Decision

### 1. What is kept (RD-D-2, RD-D-13)

The PDF passes through; the reading is kept. No PDF is stored for any period, and no text layer is stored. The only documents PortfoliFLOW keeps remain case attachments.

### 2. The reading (RD-D-3, RD-D-16, RD-D-22)

One row per document read in `document_readings`, tenant-scoped under RLS (`apply_tenant_rls`). Every signed-in user of the tenant sees all of the tenant's readings; no reading is visible across tenants.

| Group | Columns |
|---|---|
| Identity | `id`, `tenant_id`, `reading_number` (per tenant, allocated as `COALESCE(MAX) + 1` — the `cases.case_number` idiom; shown `R-nnnn`), `filename`, `sha256`, `size_bytes`, `page_count` (nullable, as reported by the model), `read_at`, `read_by`, `origin` (`upload` · `case_attachment`), `case_attachment_id` (nullable) |
| Classification | `document_type` (profile id), `profile_version`, `language` |
| Subject | `fund_name_read`, `manager_name_read`, `document_date` (nullable), `period_label`, `currency`; assignment: `investment_id` (nullable, `ON DELETE SET NULL`) or `manager_name` (nullable) — at most one of the two — with `assigned_by`, `assigned_at` |
| Findings | `findings` JSONB — per field: `key`, `label`, `value`, `type`, `unit`, `page`, `source`, `quote`, `confidence`, `origin` (`profile` · `adhoc`) |
| Reading note | `note` JSONB — `highlights` (3–7), `quotes` `[{text, page}]`, `events_and_risks`, `outlook` |
| Provenance | `model`, `prompt_version` (a hash prefix of the loaded prompt text), `adhoc_keywords` JSONB, `trust` (constant `untrusted`) |
| Search | `search_text` — plain text the writer composes from the head, the findings and the note |
| Removal | `removed_at`, `removed_by`, `removal_reason`, `purge_id` (nullable) |

Text vocabularies (`origin`, `document_type`, the confidence levels) are TEXT, enforced in the repository — the codebase's convention. A reading is roughly 10–50 KB.

Search is a case-insensitive substring match over `search_text` with wildcards escaped — the Cases idiom. No `tsvector`, no embeddings, no vector store in v1.

### 3. Profiles and the extraction contract (RD-D-4, RD-D-6, RD-D-26, RD-D-28)

Profiles are versioned repository files under `config/scraper_profiles/`, one per document type, loaded by a pure loader in `services/scraper/`. v1: `quarterly_report`, `capital_notice` (capital call / distribution notice), `pitch_deck` (pitch deck / PPM), `other`. A profile carries `id`, `version`, `label` and typed fields (`key`, `label`, `type` from the five existing keyword types, optional `unit` and hint). The field lists are those of the concept record §5.2; LP-level and fund-level figures are separate fields.

One call per document classifies it, fills the common head (document type, fund name, manager, document or reporting date, currency, language, page count), the fields of the chosen profile, the ad-hoc keywords, and the reading note. With *auto* the call carries all profiles and the model answers one; with a preselected profile it carries that one. The keyword editor remains as an override for ad-hoc questions.

A reading's document type is not editable afterwards: a wrong classification is corrected by a new upload with the profile preselected.

The call gets an explicit timeout and an explicit output budget. The three-tier JSON parser is unchanged.

Quotes are the model's reading: without a text layer they are unverified, and every surface marks them so ("as read, p. 12") beside the confidence.

### 4. Bank details (RD-D-5, RD-D-24)

Bank details are never extracted and never stored. The prompt excludes them, and a post-filter inside the pure scraper package removes account identifiers from every string of the parsed output before anything is rendered or stored. Changed bank details in a capital call are a known fraud pattern; an extracted IBAN would be a dangerous convenience.

### 5. The writer, dedup and lifecycle (RD-D-8, RD-D-9, RD-D-20, RD-D-21, RD-D-23)

- **Writer.** The route layer writes one reading per successfully extracted file when the extraction service returns — not when the result frame is delivered — so a disconnected client does not lose a paid reading. A file whose extraction failed writes nothing. A cancelled run keeps the readings of the files it completed. Without a database engine the run view works as today and nothing is written. `services/scraper/` gains no database access.
- **Dedup.** Before a run starts, each upload's `sha256` is checked against the tenant's live readings. A known document is answered with "already read on …" and an explicit *Read again*, which writes a new reading; the earlier one stays until removed.
- **Remove one.** A tombstone: identity and removal fields stay; `findings`, `note` and `search_text` are purged. A reason is required.
- **Retention run.** "Remove all readings with a document date older than X years" (read date where no document date): a preview with the count, a confirmation, one row in `reading_purges` (`criterion`, `years`, `cutoff`, `reading_count`, `run_by`, `run_at`); the affected readings become tombstones referencing it.
- **Audit.** `document_readings` and `reading_purges` carry `audit_trigger_function()` (the b033 / b034 idiom). Tombstones are kept indefinitely.
- **Not affected by removal:** anything kept into a case (copied by value, §9) and anything typed into the book.

### 6. Assignment (RD-D-7, RD-D-29)

A reading is assigned to an **investment** if the fund is held, otherwise to a **manager name** (a successor fund, a fund being followed), otherwise it stays unassigned. The assignment is proposed from `investments.name` and the known `manager_name` values and confirmed by the PM. No manager object is built. An investment's deletion leaves its readings unassigned; the fund name as read stays.

### 7. Roles (RD-D-17, RD-D-19)

| Action | Roles |
|---|---|
| See the library and a reading (Section, investment page, Shirley) | every signed-in user of the tenant, `auditor` included |
| Start a run, *Read again*, assign, remove one reading with a reason | `owner`, `member` |
| Retention run | `owner` |

"Admin" in the concept record is the tenant role `owner`. No role model is introduced.

### 8. Shirley (RD-D-10)

Three read tools in a new module under `services/tools/`, on the `case_tools.py` pattern: `list_readings` (the library's filters), `get_reading` (head, findings, note — by reading number), `search_readings` (the substring search).

All three register as **`READ_EXTERNAL_UNTRUSTED` with `wraps_result_as_untrusted=True`** and return the registry's `{source, fetched_at, body}` envelope. A reading carries GP text, and persisting it must not launder its trust level (ADR-0071 Q1): the class is the one ADR-0022 defines for content whose origin is outside the application, and its origin does not change by being stored. `services/tool_classes.py` and `services/tool_registry.py` are unchanged.

Consequence, accepted knowingly: once any of the three ran in a turn — the list included — `case_add` is locked for the rest of that turn. "Read the report and put the 80 % into the figures" does not work in one go; the path is the PM's *Keep* (§9).

`prompts/Soul_Shirley.md` stays byte-identical; the tool descriptions carry the behaviour. The `analysis_results.scraper.findings` paragraph of `prompts/Shirley_AnalysisResults_Context.md` is replaced by a pointer to the readings. The voice-call allowlist grows from 20 to 23 with one capability line. Both prompt-text changes are released by the owner from the run report before commit.

Comparisons ("average target return of the last three pitch decks") are Shirley's arithmetic over listed findings; she names the readings she used. No analytics service is added.

### 9. Cases (RD-D-11, RD-D-27)

*Keep* on the reading detail copies into a case: a quote as a kept row of the new class `reading_excerpt`, a finding as a Figures row with the existing source `document`. Copy by value: removing the reading never breaks a case or a frozen snapshot. Only the PM's Keep writes these rows — the curation that makes a quoted passage part of the PM's own file, as with `document_excerpt`.

The class's payload contract, its render and its place in the Cases search are decided in an annex ADR to ADR-0138 (expected ADR-0140), written in strand RD-3 before that strand builds.

### 10. Door 2 (RD-D-18, RD-D-30)

*Read* on a PDF case attachment writes a reading like any other (`origin = case_attachment`). It proposes the case's linked investment when the case links exactly one; otherwise the PM chooses. This is the last strand of v1 and droppable; if dropped, it moves to the Cases Round 2 package R-1 with #084. The two origin columns are part of b038 either way.

### 11. Surfaces and the name (RD-D-14, RD-D-15, RD-D-25)

The Section stays under Assistants and is named **Reading Desk** (slug `reading-desk`). It holds *Read* (the run view of today, each result block naming the reading it was saved as and proposing its assignment), *Library* (list, filters, search, reading detail, assign, remove, *Read again* — whether the detail opens in a slot row or as a sub-surface with the crumb follows `docs/ux/ui-standards.md` and is fixed on the mockup) and — for owners — *Retention*. The investment detail page gains a chronological "Documents read" list.

The rename is user-visible only: Section title, slug, UI copy, and the Admin label "Reading Desk model". `services/scraper/`, the `/scraper/…` run routes, the setting `openrouter.scraper_model` and the environment variable `SCRAPER_MODEL` keep their names, because tenants' settings and operators' `.env` files depend on them. In the glossary the *Report Scraper* remains the name of the extraction backend; the *Reading Desk* is the Section and the store of readings.

Search over readings is not content search over documents: it runs over PortfoliFLOW's own structured capture. The ADR-0138 §11 rule (no content search over attachments) is untouched.

### 12. ADR-0071's open questions, answered

| ADR-0071 question | Answer |
|---|---|
| Q1 Trust provenance | A `trust` column and the tools' class (§8); the payload reaches Shirley only inside `<external_content … trust="untrusted">`. |
| Q2 Schema home | A purpose-built `document_readings` table. No generic analysis-results store is built: one writer exists, and a generic shape would have to guess the next one's needs. |
| Q3 `kind` taxonomy | None. A second run-bound result gets its own decision. |
| Q4 Retention / lifecycle | Tombstones, a recorded retention run, audit trigger (§5). |
| Q5 Writer integration | In the route layer, when the service returns (§5); the scraper package stays pure. |
| Q6 DataStore-tool convergence | Not taken here; it stays with roadmap #035. The stale context paragraph is removed (§8). |

---

## Rationale

- **Structured capture instead of documents.** The database stays small and the product stays outside document management, while the two tests are still met: what the PM asks later is a figure, a date, a passage with its page.
- **A purpose-built table.** The reading has a subject, an assignment, a lifecycle and a search; none of that belongs in a generic JSONB store.
- **The class that matches the origin.** Registering the tools as untrusted external reads costs one same-turn convenience and needs no change to the registry or to ADR-0022.
- **One search idiom.** Substring search behaves the same in Cases and in the Reading Desk and copes with German compounds and figures.
- **Narrow scope.** Due diligence and book data are named follow-ups with their own rules; v1 stays within two to three intensive days.

## Alternatives Considered

- **Keep PDFs for a limited period.** Rejected: database growth, and the step into document management the product has refused twice (ADR-0107 §7, ADR-0138 §11).
- **A generic analysis-results store (ADR-0071's direction).** Rejected: one writer, speculative generality.
- **`READ_INTERNAL` tools that wrap their own result.** Rejected: the registry refuses the flag on that class, and wrapping inside the tool would skip the turn lock — the laundering ADR-0071 Q1 warns of.
- **`list_readings` as a trusted read.** Rejected for v1: file names and names as read come from the document; one rule for all three tools is easier to state and to test.
- **`tsvector` full-text search.** Rejected for v1: a second search idiom, and an unstemmed vector does not find parts of compounds.
- **A PDF parser for page count and a text layer.** Rejected for v1: a new dependency for a display value; revisited with roadmap #097.
- **Renaming the code identifiers.** Rejected: settings rows and `.env` files carry them.
- **Do nothing.** Rejected: the tool keeps forgetting, and five roadmap items keep circling the gap.

## Consequences

### Positive

- What was read is found again, with its page, by the PM, by a deputy and by Shirley.
- The Reading Desk becomes the pipeline for #083 (document input) and #084 (reading room), and the raw material for #080, #092 and #093.
- One audited, tenant-scoped record of what the institution read and what it removed.

### Negative / accepted

- A reading tool locks `case_add` for the rest of the turn.
- Quotes and page references are unverified in v1.
- A wrong classification costs a second upload.
- Longer model output per document: more cost and latency per run.

### Neutral / follow-ups

- #096 Reading Desk as a data source for the book (provenance, conflicts, precedence).
- #097 Text layer and quote verification, only if testing shows the need.
- Further profiles: LP letter / special report first, monthly letter (#085), AGM papers.
- #035 DataStore decommission takes the remaining `scraper` producer wording.

## Non-goals (binding)

- **Not a DMS.** No PDFs kept, no folders, no versions.
- **Not due diligence.** No pipeline or deal object, no data-room workflow (#003 gets its own concept).
- **Not a source of book data.** Nothing a reading contains reaches `investment_navs`, `investment_cashflows` or any other book table in v1 (RD-D-12).
- **No analytics service** over readings.

## Implementation Notes (pointers for the build kickoffs; not prompts)

- Migration `b038` after `b037_add_case_working_file`: `document_readings`, `reading_purges`, RLS, audit triggers; round-trip regression test on the b037 pattern.
- Model `core/models/document_reading.py`, repository `core/repositories/document_reading_repository.py`.
- Pure package `services/scraper/`: profile loader, extended models (additive), prompt and message builder, bank-detail filter; `prompts/Scraper_Prompt.md`; `config/scraper_profiles/`.
- Orchestration outside the pure package (writer, assignment proposal, retention) in a new `services/reading_desk/`.
- Web: `web/routes/scraper.py` (dedup gate, writer hook in `_run_in_thread`), a new router for library, detail, assign, remove and retention; `web/shell.py` (`SectionMeta`), `_partials/areas/_assistants_body.html`, `web/templates/investments/detail.html`, `web/routes/provider_credentials.py` (label).
- Shirley: `services/tools/reading_tools.py`, the default-tool import in `services/ai_service_core.py`, `services/voice_call/allowlist.py` and `capabilities.py`, `prompts/Shirley_AnalysisResults_Context.md`.
- Cases (after ADR-0140): `core/repositories/case_working_file_repository.py`, `web/case_keep.py`, `web/routes/cases.py`, `_partials/cases_artifact.html`, `services/cases/file_text.py`, `core/repositories/case_repository.py` (search).

## Compliance & Audit Relevance

- **ISO 25010:** functional suitability, security (confidentiality across tenants, integrity of the trust level), maintainability.
- **Regulatory references:** BAIT/VAIT traceability — who read which document when, who removed which reading and why, each retention run with its criterion and count.
- **Audit evidence:** `audit_log` rows for `document_readings` and `reading_purges`; the RLS isolation test; the tool tests for class, wrapping and the turn lock; the bank-detail filter tests.

## References

- ADR-0027, ADR-0053, ADR-0123 (Report Scraper as built); ADR-0071 (superseded); ADR-0022, ADR-0023; ADR-0035, ADR-0036, ADR-0063; ADR-0107, ADR-0138; ADR-0134; ADR-0137; ADR-0092 (`ingest_origin`, for #096).
- `docs/concepts/reading-desk-concept.md`.
- Roadmap: #095 (this item), #096, #097, #085, #021, #003, #083, #084, #080, #092, #093.

---

## Revision History

| Date       | Author                     | Change                         |
|------------|----------------------------|--------------------------------|
| 2026-11-21 | PortfoliFLOW project owner | Initial draft, status Proposed |
| 2026-11-21 | PortfoliFLOW project owner | Accepted; index row added |
