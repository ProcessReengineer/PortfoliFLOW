# ADR-0141: Cases — a Reading Excerpt in the Working File: the Kept Class `reading_excerpt` and the Figures Row of a Kept Finding

- **Status:** Accepted (2026-11-28)
- **Date:** 2026-11-26
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #095 (Reading Desk v1)
- **Amends:** ADR-0138 §9 (the kept classes — a seventh class), §10 (the file text Shirley reads — one more line form) and §11 (the searchable text of a case — one more passage), as an annex amendment. ADR-0138 stays Accepted and unedited; this ADR records the extension (ADR-0134 §3 precedence).
- **Fulfils:** ADR-0139 §9, which left the class's payload contract, its render and its place in the Cases search to an annex written in strand RD-3 before that strand builds (RD-D-11, RD-D-27).
- **Honours:** ADR-0138 §2 (frozen snapshots reference kept rows), §3 (a figure is a value and its provenance), §8 (the channels by which a file fills), ADR-0139 §5 (removing a reading does not affect what was kept into a case), §8 (Shirley's reading tools are untrusted reads; `case_add` is locked after them), ADR-0140 (a removed reading's content is gone from the database), ADR-0022/0023 (tool trust classes), ADR-0114 (the snapshot principle), ADR-0008 (English-only codebase).
- **Numbering note:** ADR-0139 §9, RD-D-27 and the concept record announce this annex as "expected ADR-0140". That number went to the audit annex (ADR-0140, which says so in its own numbering note). This is the announced annex; it carries the number 0141. ADR-0139 and the concept record are not edited.
- **Concept record:** `docs/concepts/reading-desk-concept.md` (RD-D-11, RD-D-27). **Binding mockup:** `M-RD-1_reading-desk-mockup.html` (approved 2026-11-25; it marks where Keep sits on a finding and on a quote).
- **Tags:** cases, reading-desk, keep, shirley, trust, search, snapshot, bait-vait

---

## Context

A reading (ADR-0139) holds two things a portfolio manager wants in a case: a **quote** from the reading note ("We expect to call a further 8–10% of commitments …", as read, p. 4) and a **finding** (NAV, investor's share: 41236000 EUR, as read, p. 7). ADR-0139 §9 decided that *Keep* on the reading detail copies them into a case — the quote as a kept row of a new class `reading_excerpt`, the finding as a Figures row with the existing source `document` — by value, and that only the PM's Keep writes them.

It left three things to this annex: the class's payload contract, its render, and its place in the Cases search. They cannot be taken from an existing class:

- `document_excerpt` (ADR-0138 §9) addresses a **case attachment**: its row requires an `attachment_id`, and its render links to the download. A reading has no document — the PDF passes through and is never stored (ADR-0139 §1).
- A reading can be **removed** (ADR-0139 §5), and removal purges its findings and its note for good (ADR-0140). A kept row that pointed into the reading would break; a frozen snapshot that referenced it would no longer render what was decided on.
- The passage is **GP text as a model read it**. `get_case` is a `READ_INTERNAL` tool and returns the working file as trusted content (ADR-0138 §10), while the reading itself reaches Shirley only through untrusted reads (ADR-0139 §8). What turns a passage of the one into content of the other has to be stated, not left to follow from the code.

Constraints verified against the tree (image of 2026-11-26, migration head `b038_add_document_readings`):

- The kept-class and figure-source vocabularies are frozensets in `core/repositories/case_working_file_repository.py` (`_KEPT_ARTIFACTS`, six classes; `_FIGURE_SOURCES`, with `document` among them). `case_kept.artifact` and `case_figures.source` are TEXT columns without a check constraint. **No migration is needed.**
- A snapshot references a kept row by id and `payload_sha256` for every class but `generated_document` (`core/repositories/case_snapshot.py`, `_BY_VALUE_ARTIFACT`); it embeds figures by value. A kept row is never deleted (`removed_at`).
- `search_working_file` (`core/repositories/case_repository.py`) matches a kept row's `comment`, its `caption` except on a `document` row, and — for `document_excerpt` only — `payload["passage"]`.
- `services/cases/file_text.py::_kept_line` renders a kept row as its class label and caption; an excerpt adds its quoted passage.
- Figures of the sources `typed`, `document`, `conversation` and `external` are editable rows, and a figure is hard-deletable (ADR-0138 §3). Outside `book`, no figure carries a `source_ref` today.
- A finding's value is stored ungrouped (`41236000`); thousands are grouped for display on the Reading Desk only (D-RD-2-12, D-RD-2-36).

Audit relevance: the provenance of what a decision rested on (BAIT/VAIT traceability), and the durability of a frozen file after its source has been removed.

## Decision

### 1. A seventh kept class, `reading_excerpt`

`reading_excerpt` joins the kept classes of ADR-0138 §9. Its row follows every rule of that section — placed in a thread or at file level, optional caption and curation comment, removed by `removed_at`, never deleted — with these particulars:

- **`attachment_id` is null.** The class addresses no attachment; the repository refuses one, as it does for every class outside `document` and `document_excerpt`.
- **The payload is by value and immutable once kept.** It is a copy taken from the reading at the moment of the Keep; nothing reads the reading again afterwards.
- **Snapshots reference it** by kept id and `payload_sha256`, like every class but `generated_document`. Because the row is never deleted and its payload never changes, the reference stays resolvable.

Payload contract:

| Key | Type | Content |
|---|---|---|
| `passage` | string | The quote exactly as the reading's note holds it. |
| `page` | integer or null | The page the reading names for the quote. |
| `filename` | string | The reading's file name. |
| `document_date` | `YYYY-MM-DD` or null | The reading's document date. |
| `document_type` | string | The reading's document type (the profile id). |
| `document_type_label` | string | That type's label as shown at the moment of the Keep. |
| `reading_number` | integer | The reading's number — a reference only: no foreign key, no join, never resolved to render the row. |

The caption is optional; without one it is the passage's first line, clipped — the rule of `document_excerpt`.

### 2. Who writes it: the PM's Keep, and nothing else

A `reading_excerpt` row is written by one path: a signed-in `owner` or `member` presses *Keep* on a quote of a live reading (`via = "keep"`). The repository refuses the class with any other `via`.

- `case_add` gains no kind for it, and no proposal item carries it. **Shirley never writes a `reading_excerpt`.**
- A removed reading offers no Keep: it has no quotes.
- A closed case refuses, as everywhere (ADR-0138 §9).

**This is the trust rule.** A reading is content from outside the application, and it reaches Shirley as such (ADR-0139 §8). The working file is the PM's own, and `get_case` returns it as trusted content. A passage crosses from the first to the second only by the PM's choice — the curation that already makes a `document_excerpt` part of the file. Persisting a reading did not launder its trust level (ADR-0139 §8); a write by Shirley would. The turn lock of ADR-0022 protects the same boundary inside one turn; this rule protects it across turns.

### 3. Render

In the file — live or frozen — the row renders as a quotation with its provenance:

- the passage, quoted;
- beneath it **"as read"** with the page where there is one, the file name **as plain text** — no download link, because no document is kept —, the document type's label and the document date where there is one, and the reading's label `R-nnnn`;
- `R-nnnn` links to the reading's address `/assistants/readings/<number>` (D-RD-2-22). If the reading has been removed, that address answers with identity and removal only (D-RD-2-14); the kept row is unaffected.

The words *as read* are part of the contract: the passage is the model's reading of the document and is not checked against it (text layer and quote verification are roadmap #097). The row renders from its payload alone — the Cases surface does not consult the Reading Desk or the profiles to show it. Every payload field is untrusted text and renders through autoescape only.

### 4. The file text Shirley reads

`get_case` and the brief render the row on the `document_excerpt` pattern: the class label, the caption, the quoted passage — followed by *as read*, the page, the file name and `R-nnnn`. No other payload field enters the file text.

### 5. Search

The quoted passage of a `reading_excerpt` is searched like an excerpt's passage: `search_cases` and the Cases list search match `payload["passage"]` for both excerpt classes. Caption and comment are searched as for every class. The file name, the document type, the document date and the reading number are **not** searched.

The boundary of ADR-0138 §11 and RD-D-15 holds in both directions: the Cases search finds a passage because the PM kept it as text, and finds nothing of a reading that was not kept; the search over readings stays the Reading Desk's and Shirley's `search_readings`.

### 6. A kept finding is a Figures row

*Keep* on a finding writes a row to the case's Figures (ADR-0138 §3) — no kept row, no new class, **no new figure source**:

| Field | Content |
|---|---|
| `label` | The finding's label. |
| `source` | `document`. |
| `value_text` | The **stored** value with its unit — never the grouped display string (D-RD-2-36). |
| `as_of` | The reading's document date, in the form the Figures block writes dates. |
| `source_ref` | `{"reading_number": <n>, "filename": "<file name>", "page": <p or null>}`. |
| `note` | Empty; the PM's to write. |

The Figures block shows the reference beside the source chip — `R-nnnn`, linked as in §3, and the page. From then on the row is an ordinary figure: editable and removable while the case is open or waiting, embedded by value in every snapshot (ADR-0138 §3). The same gates as §2 apply: `owner` and `member`, a live reading, an open or waiting case; Shirley's `case_add` writes no figure from a reading, because a turn that read one is locked (ADR-0139 §8).

### 7. Undo

Keep answers with a line that carries *Undo*, as in the chat and on the Planning Desk. Undo marks a kept `reading_excerpt` removed (`removed_at`) and deletes a kept finding's figure row.

### 8. Removing the reading changes nothing in a case

Removing a reading, or a retention run that takes it (ADR-0139 §5), leaves every case as it was: the kept row keeps its payload, the figure keeps its value and its reference, and a file frozen before or after the removal renders the same. Only the link behind `R-nnnn` then leads to a tombstone.

## Rationale

- **By value, because a reading can disappear and a case must not.** The reading's lifecycle (remove, retention) and the case's (freeze, close, archive) are independent by design; a reference between them would make one of the two promises false.
- **A class of its own, because an excerpt without a document is a different thing to look at.** `document_excerpt` says "this passage is in that file, open it". `reading_excerpt` says "this is what was read in a document PortfoliFLOW does not hold". Folding the second into the first would mean a nullable attachment, a render that sometimes has a link, and a search clause that has to know the difference anyway.
- **Keep-only, because the PM's choice is the only thing that changes the standing of GP text.** Everything else in ADR-0139 §8 follows from not letting stored text gain trust by being stored.
- **The existing figure source, because the provenance is the same kind.** A value taken from a document is `document` whether the PM typed it from the page or kept it from a reading; the reference says which reading and where.
- **The stored value, because the grouped form is the Reading Desk's presentation.** A case figure is compared, searched and frozen; it should be the value the reading holds.
- **The type's label by value, because a frozen file must render the same after a profile is renamed or retired,** and because Cases then needs no dependency on `config/scraper_profiles/`.

## Alternatives Considered

- **Reuse `document_excerpt` with a null attachment.** Rejected: it breaks that class's attachment rule and its render, and hides the difference between a passage of a kept document and a passage as read.
- **Reference the reading (foreign key or number) and render from it.** Rejected: removal and retention would empty kept rows and frozen files (ADR-0139 §5, ADR-0140).
- **Embed the payload in snapshots by value, like `generated_document`.** Rejected: that class is embedded because its body is edited in place; an immutable payload in a never-deleted row is as durable by reference.
- **Let Shirley keep a quote (`case_add` kind, or a proposal item).** Rejected: it is the laundering path ADR-0139 §8 closes; the turn lock would also have to be weakened for it.
- **A new figure source `reading`.** Rejected: one more value in a vocabulary every figure surface switches on, for a distinction the reference already carries.
- **Search file name, type and date of a kept excerpt.** Rejected: the searchable text of a case is what the PM wrote or kept as text (ADR-0138 §11); a file name is neither, and `document` rows already leave it out for that reason.

## Consequences

### Positive

- A quote and a number from a GP document reach the case with one gesture and with their provenance attached.
- A case is self-contained: its file and its frozen milestones do not depend on the Reading Desk's retention.
- Shirley can use a kept passage as part of the file — and only what the PM kept.

### Negative / accepted

- **Kept content outlives the reading.** A retention run does not remove a passage a PM kept into a case; a kept row is never deleted, so a passage that names a person stays in `case_kept` after the row is removed from the file. This is the standing property of `document_excerpt` and of attachments, and it is the intent of ADR-0139 §5.
- A kept figure shows the ungrouped value (`41236000 EUR`), as stored. The PM may edit the row.
- A kept passage is the model's reading, not a verified citation; the render says so and nothing checks it (#097).
- "Read the report and put the 80 % into the figures" stays a two-step gesture: Shirley reads, the PM keeps (ADR-0139 §8).

### Neutral / follow-ups

- The class list of ADR-0138 §9 is seven long; the count in the docstrings of `CaseKept` and `CaseKeptRepository.keep` moves with it.
- Figures of the source `document` can now carry a `source_ref`; a typed `document` figure still carries none.
- Taking a kept figure into the book is roadmap #096 and unaffected.

## MC to confirm (draft only — this section is resolved and removed at filing)

| # | Proposal in this draft | Why it is open |
|---|---|---|
| 1 | The payload key for the quote is **`passage`** (§1), the key `document_excerpt` uses. | The kickoff says "the quote"; one key for both excerpt classes keeps search, file text and projection on one name. The alternative is `quote`. |
| 2 | The payload carries **`document_type_label`** beside the profile id (§1, §3). | One key more than the kickoff lists. Without it the render resolves the label from the profiles on every view, a frozen file changes when a profile is renamed, and Cases gains a dependency on the Reading Desk. |
| 3 | The payload carries **no fund or manager name**; the PM's caption is where a kept quote is named (§1). | `fund_name_read` is the model's reading; the kickoff does not list it. Adding it would make a kept quote more self-explanatory in a case that follows several funds. |
| 4 | **Caption default** is the passage's first line (§1), not the file name. | A caption is searched for every class but `document`; a file-name caption would make file names searchable through the back door. |
| 5 | A kept finding's **`as_of` is empty when the reading has no document date** (§6) — the read date is not substituted. | The retention run falls back to the read date; for a figure, "as of the day it was read" would be a wrong statement about the number. |
| 6 | **Keep is offered on found findings of the types Number, Percentage, Date and Text; a `List` finding offers none** (§6). | A list (key persons, track record) is not a figure. The alternative is to keep it as one multi-line value. |
| 7 | The finding's **own quote is not copied** into the Figures row; `note` stays empty (§6). | `note` is searched and is the PM's field. The alternative puts the supporting quote there, which keeps it after the reading's removal. |
| 8 | `R-nnnn` **links to the reading** from a kept row and a figure, also in a frozen file (§3, §6). | "A reference only" could be read as "shown, not linked". D-RD-2-22 names the address as the target of a case's link back; a tombstone answers calmly. |

## Implementation Notes (pointers for the build prompt P-RD-3b; not prompts)

- `core/repositories/case_working_file_repository.py`: `reading_excerpt` in `_KEPT_ARTIFACTS`; the Keep-only rule on `via` in `CaseKeptRepository.keep`; `_ATTACHMENT_ARTIFACTS` unchanged. `core/models/case.py`: docstring only. No migration.
- `web/case_keep.py`: Keep, picker and Undo for the Reading Desk through the shared helpers; `web/routes/reading_desk.py` and the two partials `reading_desk_findings_table.html` and `reading_desk_note.html` behind a context flag that only the reading's page sets (D-RD-2-41).
- `web/routes/cases.py` (`_project_kept_payload`, the kept row's chip, the figure's reference label) and `_partials/cases_artifact.html`; `services/cases/file_text.py::_kept_line`; `core/repositories/case_repository.py::search_working_file` (the passage clause for both excerpt classes).
- Tests: the payload contract and its hash; the class refused with `via` other than `keep` and with an attachment; `case_add` has no kind for it; the kept row and the figure survive the reading's removal and render in a file frozen before it; the search finds a kept passage and nothing of an unkept reading; a `<script>` planted in a quote, a finding's label and a file name stays inert in the case; `auditor` sees no Keep; a closed case refuses.

## Compliance & Audit Relevance

- **ISO 25010:** functional suitability (provenance of kept evidence), reliability (a frozen file independent of retention), security (integrity of the trust boundary between external text and the PM's file).
- **Regulatory references:** BAIT/VAIT traceability — what a decision rested on, who kept it and when; the kept row's `kept_by`, `kept_at`, `via` and `payload_sha256`.
- **Audit evidence:** `case_kept` rows of the class with their payload hash; the snapshot references; the repository test on `via`; the removal test named above.

## References

- ADR-0138 (§2, §3, §8–§11), ADR-0139 (§1, §5, §8, §9), ADR-0140, ADR-0114, ADR-0022, ADR-0023, ADR-0134 (§3).
- `docs/concepts/reading-desk-concept.md` (RD-D-11, RD-D-15, RD-D-27); M-RD-1 (`#decisions`: D-RD-2-12, D-RD-2-14, D-RD-2-22, D-RD-2-36, D-RD-2-41).
- Roadmap #096 (a figure into the book), #097 (text layer and quote verification), #084 (the reading room).

---

## Revision History

| Date       | Author                     | Change                                      |
|------------|----------------------------|---------------------------------------------|
| 2026-11-26 | DC-RD-3 (daughter chat)    | Draft for the owner's release, status Proposed |
| 2026-11-28 | PortfoliFLOW project owner | Accepted; index row added                   |
