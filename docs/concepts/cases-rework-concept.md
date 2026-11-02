# Cases rework — concept record

- **Status:** concept closed 2026-11-02 (owner + MC); feeds the ADR-0107 successor and the build kickoff
- **Owner:** Soenke · **Written by:** the Cases concept chat (MC), 2026-11-02
- **Supersedes in spirit:** ADR-0107 §1 (three surfaces), §2 (object model, where extended), §4 (states), §5 (tooling principle); ADR-0107 itself stays Accepted and unedited — the successor ADR records the revisions
- **Companions:** `M-CS-2_cases-mockup.html` (binding mockup, v2), `Workflow-gaps_register_2026-11-02.md` (live cases LC-1…LC-5, gaps G-1…G-15 and C-1…C-14), this record
- **Locations:** this record goes into the repository as `docs/concepts/cases-rework-concept.md` (placed by the operator). Everything else of the Cases track stays outside the tree: MC artefacts (kickoffs, plan, register, closing reports) under **`~/Code/PortfoliFLOW/cases/`**, the mockups under `~/Code/PortfoliFLOW/mockups/` (as with M-WD-1).

---

## 1. Why

Cases (ADR-0107, shipped 2026-07-22 as #051) was the first answer to "what do we do with this information now?" — built when PortfoliFLOW displayed findings but offered no place to act on them. Transactions (#061) later answered the half of that question that turns into orders. What Cases was meant to hold — the deliberation before and around the action — it holds today as an **archive with a form in front**: an append-only timeline fed through pin dialogs with mandatory curation comments, no live view of the question's subject, no view of the tickets that answered it, no knowledge of cases on Shirley's side, three list sections of which two are nearly empty. The audit rigidity that only the record needs was applied to the whole surface. Of the four things that distinguish a case from a to-do item — system-born origin with frozen evidence, a live mirror of the subject, an audit-grade record, machine-readable linkage to the action taken — two were built (origin, record) and two were not (live mirror, linkage). The result is a surface nobody would choose to work in and nobody minds. The ideas underneath are right; the surface is half built.

## 2. Definition

> A **Case** is a question or task about the book that the user keeps open **longer than one conversation** — persisted, updated by the program while it is open, and retrievable at any time. Shirley's chat is the session; the case is the memory.

Three kinds share one structure and are not modelled as types in v1 (a label is an open point): a **question** worked to a decision (re-up, secondary sale, limit breach), a **task** with an output (AGM preparation, committee paper), a **watch** that may close with "nothing to do". A case may rest: **Waiting** until a date or until the program notices something on its subject.

## 3. The two tests a case must pass

1. **Would the PM rather work here than in Excel, pen and paper?** Passes when the case gives something back — the book's own figures, live and current; provenance on every number; Shirley briefed by the file without being told; artefacts kept with one gesture; the file frozen at decision — and when nothing costs more than one gesture.
2. **Can a deputy continue where the owner left off?** Passes when every case has the same outline, every figure and artefact says where it came from and from when, every thread states its status and conclusion, open points carry owner and date, and hand-over is a milestone.

The reviewer/auditor is a beneficiary, not a persona: the record is a by-product of the work, never the work. The Journal serves the reviewer for now (export and period views are deferred).

## 4. Structure

### 4.1 The working file (mutable, autosaved, no dialogs)

Fixed outline; empty sections are hidden in the file and listed grey with "+" in the outline on the left.

| Section | Holds | Written by |
|---|---|---|
| **The question** (or **The task**) | title, why now, origin (immutable finding embed, or manual description), counterparty | PM; pre-filled from a finding |
| **Where we stand** | the subject's current state vs. at opening (Watch Desk resolution path), linked investments with live figures, linked tickets with state, system lines since opening | the program only — nothing typed, nothing persisted |
| **Threads** *(working name; alt. Questions)* | one sub-question or hypothesis each: prose, kept artefacts (chart, document excerpt, consultation, scenario snapshot, conversation), status *open · confirmed · parked · refuted*, one-line conclusion | PM; Shirley on request |
| **Figures** | named figures side by side: value, source chip (*book · typed · document · kept · conversation · external*), as-of; book rows update themselves, typed rows keep their date; no formulas | PM; Shirley on request; book |
| **Conversations** | calls and meetings: counterparty, date, what was said, what was promised | PM (typed; dictation later, G-7) |
| **Open points** | checklist with owner and due date; "ticket from this point"; reads as "questions to ask" on a task | PM; Shirley on request |
| **Decision** | options (chosen one marked), rationale, decided by/at — a draft until recorded | PM |

Kept artefact classes: the four existing (`document`, `scenario_snapshot`, `consultation`, `chart_snapshot`) plus **document excerpt** (attachment + quoted passage + page) and **generated document** (a Shirley memo with its sources, editable once accepted).

### 4.2 The record (append-only — today's `case_entries`)

Written only at **milestones**: opened · handed over · waiting set/lifted · decision recorded · closed — plus **system lines** from the book, the Watch Desk, the press watch and Transactions (finding's subject back within its ceiling, NAV restated, press cluster on a linked investment, ticket booked against the case, Shirley job requested/landed, waiting case woken). Each milestone **freezes the working file as it stands** (a snapshot entry — the ADR-0107 C5 / ADR-0114 principle applied to the whole file). A closed case renders its last frozen state; nothing on a closed case reads the book.

### 4.3 States

`open` · `waiting` (with `wait_until`; returns to open on the date or earlier on a system event on its subject) · `closed` (mandatory closing note, exactly once). Decision is optional in all kinds.

### 4.4 Links

Finding (optional, as today) · investments (n) · counterparty (free text, suggested from `manager_name`) · tickets (the existing `trade_tickets.case_id`, now shown on the case) · documents (attachments, caps as today).

## 5. How the file fills

**Rule:** the PM selects what is in the file; the cost of selecting is one gesture. Never silent extraction from chat.

1. **Type in place** — every section is editable text or a list with an "add a …" line; the primary path.
2. **Keep from the dock** — every Shirley answer (text, chart) carries *Keep →*; one click into the active thread (shown as target in the dock), a second click to choose another target. No comment, no dialog.
3. **Shirley on request** — "put the 80 % into the figures", "open a thread for the fee question", "note the call: …" — a write tool scoped to the file's structure; writes only what was asked in this turn; every write appears in the dock as a line with undo. Shirley may propose, never acts on her own initiative.
4. **The program itself** — Where we stand and system lines fill without a gesture.

Plus **proposals**: on "summarise this into the file" or when a background job lands, Shirley delivers a proposal package (thread conclusion, figures, points, memos) accepted **item by item**, editable inline; "make this a case" from the dock drafts question and first thread for the PM to edit. Nothing is written until accepted.

## 6. Shirley's role

Docked beside the case with **the file as her brief** (the existing stash, extended). Read tools: list open cases, get a case, **search cases including closed ones** (the archive as institutional memory — "have we had a gated fund before?"). Optional write tool (first `WRITE_INTERNAL` tool of the platform, ADR-0022 §4 gets its subject). Long-running jobs (AGM pack) land in the case as proposals — the case is the inbox; the job runner itself is a workflow-round item (G-12), the landing place is Cases scope.

## 7. Boundaries restated

- **Not a DMS.** No folders, no versions, no content-search UI over attachments (ADR-0107 §7 holds). A bounded **reading room** — documents Shirley reads on request within one case, results kept as excerpts and figures — is a Shirley capability (G-8), not a search function, and comes with the workflow round.
- **No analytics in Cases.** Where we stand *reads* the Watch Desk resolution path, investment reads and Transactions; nothing new is computed; Figures lay numbers side by side and do not calculate. Trade what-ifs stay in the impact panel and the Planning Desk and come back as snapshots.
- **Irene opens and closes nothing — but informs.** System lines are mechanical (`actor = system`), never Irene prose. Revises ADR-0107 "never touches a case".
- **No scenario re-hydration, no provider UI** (unchanged).
- **The record never changes.** Only the working file does, and only until the next milestone freezes it.

## 8. Delta against today (summary; the plan file carries the strands)

| Component | Today | Target | Change |
|---|---|---|---|
| `cases`, `case_entries`, `case_attachments`, pin classes, `opened_case`, frozen materiality, RLS, Journal projection, band gate, open-case endpoint | built (b031, ADR-0107/0114/0120) | unchanged; `case_entries` is the record | keep |
| Working-file tables (threads, figures, points, conversations, decision draft, prose), freeze snapshots | — | new tables, migration **b037** (b035 reserved for engagements, b036 is the Watch Desk refinement) | new |
| States | open · closed | open · waiting(until) · closed; wake-up in the tick | extend |
| Links | finding | + investments, counterparty, tickets (back-link), documents | extend |
| Where we stand | — | read-only composition over existing services | new |
| System lines | `system` writes `opened` only | book, Watch Desk, press, Transactions, scheduler write to the record | extend |
| Pin dialogs (chat, Planning Desk), mandatory comment | 2 dialogs | *Keep →* without dialog; + excerpt, + generated document | simplify |
| Shirley | brief stash, no tools | file as brief; read tools incl. closed; optional write tool; proposals | extend |
| IA | 3 sections + stand-alone detail page | 1 section; detail as sub-surface with crumb; empty sections hidden | rebuild |
| Decision | decision + rationale | options, chosen, rationale, decided by/at; draft until recorded | extend |
| Hand-over | — | owner change as milestone | new, small |
| Inbox for jobs | — | landing place + proposals (runner is G-12) | new, staged |

## 9. Decisions of record proposed for the successor ADR (C-D-n)

- **C-D-1** Definition (§2) and the two tests (§3) are the ADR's context; the reviewer is a beneficiary, not a persona.
- **C-D-2** Two layers: a mutable working file and an append-only record; milestones freeze the file as a snapshot entry.
- **C-D-3** Outline fixed as §4.1; empty sections hidden; no case kinds as a type in v1.
- **C-D-4** States open · waiting · closed; `wait_until`; wake on date or on a system event on the subject.
- **C-D-5** Links: investments (n), counterparty (text), tickets (back-link), finding, documents.
- **C-D-6** Where we stand is read-only composition over existing resolution/read paths; never persisted; becomes "At close" on a closed case.
- **C-D-7** System lines: mechanical, `actor = system`; Irene prose never; sources enumerated (falling edge, NAV restated, press cluster, ticket booked, job requested/landed, waiting lifted).
- **C-D-8** Fill channels (§5); no silent extraction; proposals accepted item by item.
- **C-D-9** Keep replaces the pin dialogs; curation comment optional; kept classes + excerpt + generated document.
- **C-D-10** Shirley: file as brief; read tools incl. closed cases; write tool optional and scoped; ADR-0022 §4 trust class `WRITE_INTERNAL`.
- **C-D-11** DMS boundary restated: reading room is a Shirley capability outside this ADR; no content-search UI.
- **C-D-12** One Section, detail as sub-surface; UX strand A-3 becomes the acceptance walk, not a separate restyle.
- **C-D-13** Hand-over as a milestone; owner is the only assignment concept.
- **C-D-14** Decision record carries options and the chosen one (restores ADR-0107 §4's "alternatives assessed").

## 10. Open points for the ADR chat (not for MC)

Thread vs. Questions naming · exact freeze payload (whole file vs. per-section deltas) · whether a waiting case's wake events are configurable · whether `description` on `cases` folds into the question prose · test seam for the editable file (autosave endpoints) · the generated-document class's storage (payload JSONB vs. attachment) · a11y of in-place editing.

## 11. References

ADR-0107, 0114, 0120 (Cases as built); ADR-0085, 0088, 0089, 0116, 0133 (findings, Watch Desk); ADR-0104 (stateless scenarios); ADR-0128 §7 (`case_id` on tickets); ADR-0050 (ephemeral chat history); ADR-0022/0023 (trust classes, Fetcher); ADR-0134 (Section as one view). Live cases LC-1…LC-5 and gaps in the register. Mockups M-CS-1 (v1, superseded) and M-CS-2 (binding).
