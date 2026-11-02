# ADR-0138: Cases — the Working File

- **Status:** Accepted (2026-11-02)
- **Date:** 2026-11-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #078 (Cases rework: the working file)
- **Supersedes in part:** ADR-0107 §1 (the three surfaces), §2 (object model —
  extended, not replaced), §4 (two states), §5 (consult-and-pin, zero embedded
  tools); **revises** the Irene rule of ADR-0107 §3 ("never opens, touches, or
  comments on a case"). ADR-0107 itself stays Accepted and unedited, as do
  ADR-0114 and ADR-0120; this ADR records the revisions (ADR-0134 §3
  precedence: a stale ADR is corrected by a successor, never by an edit).
- **Honours:** ADR-0085 (findings immutable; `opened_case` resolution kept),
  ADR-0088 (synthesis contract untouched), ADR-0089 (calm by default — now
  applied to system lines), ADR-0104 (stateless scenarios — snapshots stay
  evidence), ADR-0114 (snapshot principle — now applied to the whole file),
  ADR-0022/0023 (tool trust classes; the Fetcher discipline), ADR-0050
  (ephemeral chat — the reason the case is the memory), ADR-0117 (the tick
  runner — the home of the waiting wake-up), ADR-0128 §7 (`trade_tickets.case_id`,
  the provenance chain), ADR-0133 §5/§6 (Journal as projection; the brief
  stash), ADR-0134 §4 (Section as one view), ADR-0008 (English-only codebase).
- **Concept record:** `docs/concepts/cases-rework-concept.md` (closed
  2026-11-02; decisions C-D-1…C-D-14 recorded there and carried here).
  **Binding mockup:** `M-CS-2_cases-mockup.html` (kept outside the tree; where
  mockup and this ADR disagree, this ADR wins, then the code).
- **Tags:** cases, shirley, irene, watch-desk, transactions, workflow,
  audit-trail, tools, ui, schema, bait-vait

---

## Context

### The diagnosis

Cases shipped on 2026-07-22 (ADR-0107, roadmap #051) as the first answer to
the question the Watch Desk raises and cannot answer: *what do we do with this
information now?* It was built when PortfoliFLOW displayed findings and
offered no place to act on them. Transactions (ADR-0128, #061) later answered
the half of that question that turns into orders. What Cases was meant to hold
— the deliberation before and around the action — it holds today as **an
archive with a form in front of it**: an append-only timeline fed through two
pin dialogs with a mandatory curation comment each; no live view of the
question's subject; no view of the tickets that answered it; no knowledge of
cases on Shirley's side (she receives a brief, she has no tool); three list
sections of which two are nearly empty. The audit rigidity that only the
record needs was applied to the whole surface.

Of the four properties that distinguish a case from a to-do item — a
system-born origin with frozen evidence, a live mirror of the subject, an
audit-grade record, machine-readable linkage to the action taken — two were
built (origin, record) and two were not (live mirror, linkage). The result is
a surface nobody would choose to work in and nobody minds. The ideas
underneath are right; the surface is half built.

ADR-0107's cuts were deliberate release-scope decisions (the last feature set
before the AGPL release: "complete in itself, nothing promised"), and the
Gate-C0 decisions of its build (no linked investments, decision E; no Journal
table; plural table names) were correct for that scope. None of them is
reopened as a mistake; they are extended because the product has moved: the
Watch Desk refinement (ADR-0133) made findings explainable, Transactions made
actions bookable, the UX overhaul (#070) made every Area one Section per
view, and Shirley gained the tools to read the book.

### The evidence base

The concept phase walked five live cases from the owner's own practice, at the
edge of what the program can do today (register:
`Workflow-gaps_register_2026-11-02.md`, kept with the track's artefacts
outside the tree):

- **LC-1** — a GP offers Fund V; re-up? A prior fund's anomaly is traced
  through an FX hypothesis to a hedging undertaking in the PPM. Manual, slow,
  counterparty-centred.
- **LC-2** — the listed-equity quota is over its ceiling; trim the ETF, book
  the ticket, close. From a finding, fast (the mockup's CASE-0027 / CASE-0033).
- **LC-3** — an institutional real-estate fund drops without visible cause;
  the press reports suspended redemptions; the GP's special report explains
  liquidity; the decision is to coordinate with other investors and defer —
  then wait and revisit. Event-driven, slow, with a waiting phase.
- **LC-4** — due diligence over a data room: anything unusual, stated versus
  our own performance, portfolio fit. Manual, document-heavy.
- **LC-5** — AGM preparation: "put together everything I need" — a memo per
  fund, current figures, a press review. A task with an output, multi-step.

The walks produced two lists: fourteen gaps inside Cases (C-1…C-14, this ADR)
and fifteen gaps in the surrounding instruments (G-1…G-15, now roadmap
#079–#093, a separate workflow round after this build). The split is the
boundary of this ADR: it changes what a case *is* and how it fills; it does
not add instruments to the platform.

### The two tests

The concept fixed two tests a case must pass, and they are the design's
acceptance criteria:

1. **Would the PM rather work here than in Excel, pen and paper?** Passes
   when the case gives something back — the book's own figures, live and
   current; provenance on every number; Shirley briefed by the file without
   being told; artefacts kept with one gesture; the file frozen at decision —
   and when nothing costs more than one gesture.
2. **Can a deputy continue where the owner left off?** Passes when every
   case has the same outline, every figure and artefact says where it came
   from and from when, every thread states its status and conclusion, open
   points carry owner and date, and hand-over is a milestone.

The reviewer, auditor or supervisor is a **beneficiary, not a persona**: the
record is a by-product of the work, never the work. The Journal (ADR-0133 §5)
serves the reviewer for now; export and period views are deferred.

### Constraints verified against the tree (image of 2026-11-02, version `2026.10.1`)

- `cases` · `case_entries` · `case_attachments` (migration b031, RLS by
  `apply_tenant_rls`); the only permitted mutation of a `cases` row is the
  close transition; entries are append-only; `payload` is JSONB opaque to
  persistence; the `actor` vocabulary is `pm` · `shirley` · `system`, and
  `system` has exactly one writer today — the open-from-finding composition
  (`web/routes/watch_desk.py`).
- Migration head is b036; **b035 is reserved** for `engagements` (ADR-0129,
  Stage B). This ADR's migration is **b037**.
- `trade_tickets.case_id` exists (ADR-0128 §7) and is written from the
  Transactions case picker; nothing on the case reads it back.
- Shirley has sixteen registered tools, fifteen `READ_INTERNAL` and one
  `READ_EXTERNAL_UNTRUSTED`; the `WRITE_INTERNAL` class of ADR-0022 has no
  member. The case brief reaches her through the session stash
  (`ActiveBrief`, `_compose_case_brief` in `web/routes/chat.py`), composed
  from the same projections the detail page renders (ADR-0107 C6 binding
  decision 4; extended by ADR-0133 §6 with the briefing kind).
- The Watch Desk's resolution path (`services/watch_desk/family_status.py`,
  the overlay) and the investment reads behind `get_investment_data` are the
  read paths "Where we stand" composes over; nothing in `services/analytics`
  is touched.
- The scheduler's shared tick runner (`services/scheduler/tick_runner.py`,
  ADR-0117 §2) is the natural home of a date-driven wake-up.
- `investment.manager_name` is free text — the source of counterparty
  suggestions.
- The Section catalogue (`web/shell.py`) lists Cases with three Sections;
  the sidebar is hardcoded; `cases_detail.html` is a stand-alone page
  extending `base.html`, outside the shell's Section model (ADR-0134 §4).
  `cases.css` (1 060 lines) still carries the retired `pf-dc-*` vocabulary's
  last two classes and a scoped `pf-chip` homonym (`docs/ux/ui-standards.md`
  §5 items 5, 14, 15).

This decision is audit-relevant (BAIT/VAIT: documented decision files,
immutable records, containment of LLM-driven writes — ADR-0022).

## Decision

### Decisions carried in

| Concept decision | Section | ADR-0107 clause |
|---|---|---|
| C-D-1 definition and the two tests | §1 | supersedes §1 "what the feature is" |
| C-D-2 two layers, milestones freeze | §2 | supersedes §2 "append-only timeline" as the whole model |
| C-D-3 outline, hidden sections, no types | §3 | supersedes §1 "case detail" |
| C-D-4 states and wake-up | §4 | supersedes §4 |
| C-D-5 links | §5 | extends §2; reverses Gate-C0 decision E |
| C-D-6 Where we stand | §6 | new; confirms §5 "no analytics" |
| C-D-7 system lines, Irene revision | §7 | revises §3 Irene rule; refines §2 actor `system` |
| C-D-8 fill channels, proposals | §8 | supersedes §5 consult-and-pin |
| C-D-9 Keep, kept classes | §9 | supersedes §2 pin anatomy and §5 pin dialogs; extends ADR-0114 §2 |
| C-D-10 Shirley: brief, read tools, write tool | §10 | supersedes §5 "zero embedded tools"; refines C6 brief |
| C-D-11 DMS boundary | §11 | confirms §7 |
| C-D-12 one Section, A-3 | §12 | supersedes §1 "three surfaces" |
| C-D-13 hand-over | §13 | supersedes Non-goal "no assignment mechanics" |
| C-D-14 decision record | §14 | restores §4 "alternatives assessed" |

### 1. What a case is

> A **Case** is a question or task about the book that the user keeps open
> **longer than one conversation** — persisted, updated by the program while
> it is open, and retrievable at any time. Shirley's chat is the session; the
> case is the memory.

Three kinds share one structure: a **question** worked to a decision (re-up,
secondary sale, limit breach), a **task** with an output (AGM preparation, a
committee paper), a **watch** that may close with "nothing to do". They are
**not modelled as types** (§3). A case may rest: **Waiting** until a date or
until the program notices something on its subject (§4). A case may close
with "looked at Funds I–III, all within expectations, nothing to do".

The two tests above are the ADR's acceptance criteria and the acceptance
walk's script (§12). The reviewer is a beneficiary: nothing in the working
file exists for the reviewer's sake; the record (§2) is what the reviewer
reads, and it is written as a by-product of milestones the PM reaches anyway.

*Why.* ADR-0107 defined a case as "an open question worked to a documented
close" and built the documentation. The walks show the PM's actual work —
notes, side calculations, calls, half-finished thoughts continued on the
train — happens *between* the documented points, today in Excel and on
paper. A case that does not hold that work is not where the decision is made,
and a record of a decision made elsewhere is an archive. Supersedes ADR-0107
§1 "what the feature is".

### 2. Two layers: the working file and the record

A case has two layers with different mutability:

- **The working file** — mutable, autosaved, no dialogs. Everything the PM
  types, keeps or links. It lives in working-file tables (§Object model) and
  changes freely while the case is open or waiting.
- **The record** — append-only, today's `case_entries`. Written only at
  **milestones**: opened · handed over · waiting set · waiting lifted ·
  decision recorded · closed — plus **system lines** (§7). Every milestone
  **freezes the working file as it stands** into a `frozen` entry written in
  the same transaction as the milestone entry.

A closed case renders its last frozen state and nothing else; nothing on a
closed case reads the book (§6). The working-file rows of a closed case stay
in their tables, read-only, so that search (§12) and Shirley's `search_cases`
(§10) read closed cases without unpacking snapshots.

The freeze payload is the **whole file** (not per-section deltas, see
Alternatives): a JSON document of the question, the Where-we-stand readings
as rendered, every thread with its status and conclusion, every figure with
its value at the moment, conversations, open points, the decision(s), and the
kept artefacts **by reference** (`case_kept.id`, class, caption, comment, the
payload's `sha256`, the attachment id where one exists). Kept artefact
payloads are immutable rows (§9), so a reference into them is as good as a
copy and the snapshot stays self-contained within the case's own tables —
the ADR-0114 rationale (no reference into an ephemeral or shared store)
holds. Attachment bytes are never copied into a snapshot.

*Why.* The audit rigidity ADR-0107 §2 applied to every entry is right for the
record and wrong for the work: a PM who cannot edit a half-finished thought
writes it elsewhere. Separating the layers keeps the record's guarantees
exactly as they are (append-only, never edited, never deleted — ADR-0107 §2
confirmed for `case_entries`) and gives the file the freedom a desk has.
Freezing at milestones is ADR-0107 C5 / ADR-0114's snapshot principle applied
to the whole file instead of to single artefacts: the record shows what the
decision-maker saw at decision time. Supersedes ADR-0107 §2 "append-only
timeline" as the model of the case; confirms it for the record.

### 3. The outline — fixed, with empty sections hidden

Every case has the same outline, in this order:

| Section | Holds | Written by |
|---|---|---|
| **The question** (or **The task**) | title, the prose (why now, what is asked), origin (immutable finding embed with the materiality at opening, or the manual description), counterparty | PM; pre-filled from a finding |
| **Where we stand** | the subject's current state against its state at opening, linked investments with live figures, linked tickets with state, system lines since opening | the program only (§6) |
| **Threads** | one sub-question or hypothesis each: prose, kept artefacts, status *open · confirmed · parked · refuted*, a one-line conclusion | PM; Shirley on request |
| **Figures** | named figures side by side: value, source chip (*book · typed · document · kept · conversation · external*), as-of; book rows update themselves, typed rows keep their date; no formulas | PM; Shirley on request; the book |
| **Conversations** | calls and meetings: counterparty, date, what was said, what was promised | PM (dictation is #091) |
| **Open points** | a checklist with owner and due date; "ticket from this point"; reads as **Questions to ask** on a task | PM; Shirley on request |
| **Decision** | options with the chosen one, rationale, decided by/at — a draft until recorded (§14) | PM |
| **Record** | the append-only layer (§2), milestones and system lines, newest last | the program |

Empty sections are **hidden in the file** and listed grey with "+" in the
outline on the left; a case opened from a finding shows three blocks (the
question, Where we stand, the record) and stays light. The sub-unit of
Threads is called **Thread**. The owner rejected the German working term
"Spuren"; of the two English candidates the ADR chooses *Threads* over
*Questions* because a thread may be a hypothesis, not a question ("an open
currency position?"), because *Questions* would collide with the section
heading **The question** and with **Questions to ask** (the open points of a
task), and because a thread is the unit that *carries* kept artefacts — the
word names a line of inquiry with what was gathered along it, which is what
the section holds. The UI vocabulary is English (ADR-0008); a German term is
not this ADR's concern.

**No case kinds as a type in v1.** No `kind` column, no templates, no
per-kind outline, no filter. The only presentational difference between a
question and a task is the heading (**The question** / **The task**) and the
label of the open-points section (**Open points** / **Questions to ask**);
it is carried by one nullable presentation flag `cases.framing`
(`question` · `task`, default question) that changes no behaviour. A "watch"
is not a framing: it is a case that is waiting (§4).

*Why.* A fixed outline is the deputy test: the second reader finds the same
places in every case. Hiding empty sections is the PM test: the fast case
(LC-2) must not carry the structure of the slow one (LC-1). Types were
rejected because the five walks share one structure and differ only in which
sections fill; a type would decide for the PM what the case will need before
the work shows it. Supersedes ADR-0107 §1 "case detail — origin, timeline,
rail".

### 4. States: open · waiting · closed, and the wake-up

`cases.state` ∈ `open` · `waiting` · `closed` (TEXT, application-enforced as
today).

- **Waiting** carries `wait_until` (a date). The PM sets it ("Wait until…")
  with an optional note; setting and lifting are milestones (`waiting_set`,
  `waiting_lifted`) and freeze the file. A waiting case is a list state of
  its own (§12): it is out of the Open list and listed under *Waiting* with
  its date.
- A waiting case **returns to open on its date** — the shared tick runner
  (ADR-0117 §2) lifts it on the first tick of that day — **or earlier when a
  system event of §7 fires on its subject** (the finding's subject back
  within its ceiling, a NAV restated on a linked investment, a press cluster
  on a linked investment, a ticket booked against the case). The lift is a
  `waiting_lifted` milestone with `actor = system` and the reason in its
  payload (`date` or the event); the system line that woke it is written
  too. A PM may lift the wait by hand (`actor = pm`).
- The wake events are **not configurable** in v1: the four sources are the
  case's subject by construction (its finding, its investments, its tickets).
- **Closed** is reached exactly once, with the mandatory closing note
  (ADR-0107 §4 confirmed). Closing writes `closed` and the final `frozen`
  entry; the Journal projection of a closed case is unchanged (ADR-0133 §5).
  The decision is optional in all kinds.

*Why.* LC-3's "defer the redemption, revisit in six months" leaves a case
neither open (nothing to do) nor closed (not resolved). ADR-0107 §4's
"nothing else than Open → Closed" kept the workflow machine invisible; one
more state with a date keeps it so — Waiting has no transitions of its own
beyond entering and leaving, and the program leaves it for the PM, never
the other way round. Supersedes ADR-0107 §4 "two states"; confirms its
closing-note rule.

### 5. Links

A case links to:

- **its finding** — optional, as today (`finding_id`; the finding is never
  mutated, ADR-0085; the origin embed and the materiality at opening are
  unchanged, ADR-0107 §3/C4, ADR-0120 band gate unchanged);
- **investments (n)** — a link table; linked by the PM ("Link investment…"),
  pre-linked from a finding where the finding's subject names investments;
- **a counterparty** — free text on the case (`cases.counterparty`), with
  suggestions from `investment.manager_name` of the book; no counterparty
  entity (the manager view is #080);
- **tickets** — the existing `trade_tickets.case_id` (ADR-0128 §7), now read
  back and shown on the case with state; an open point may create a ticket
  ("Create ticket from this point"), which links the ticket to the case and
  the point to the ticket;
- **documents** — attachments as today (`case_attachments`, caps as today),
  now addressed through kept artefacts of class `document` or
  `document_excerpt` (§9) instead of pin entries.

*Why.* Every live case had a subject the program knows (an investment, a
manager, a ticket) and the case could not name it (C-1…C-3). Where we stand
(§6) needs the links to read anything; the deputy needs them to know what
the case is about. Extends ADR-0107 §2; reverses Gate-C0 decision E
(investment links) on the evidence of the walks.

### 6. Where we stand — read-only composition

**Where we stand** is composed at render time from existing read paths and
is **never persisted** (except as part of a milestone's frozen snapshot):

- the finding's subject today against its state at opening — the Watch Desk
  resolution path (`family_status`, the overlay) for a case from a finding;
  "at opening" is what the `opened` milestone's snapshot recorded;
- the linked investments with their live book figures (commitment, NAV,
  multiples, IRR, last reported as-of) from the investment reads Shirley's
  `get_investment_data` already uses; exposure to the counterparty where
  the linked investments share a manager; where a linked investment's
  currency differs from the book currency, the FX rate at opening and now
  from the FX repository;
- the linked tickets with their lifecycle state;
- the system lines written to the record since opening (§7), so the block
  reads "since you opened this case: …".

The composition may lay two readings side by side and show their difference
(a quota at opening and today); it computes **no metric the book does not
already compute** and nothing enters `services/analytics`. On a **closed**
case the block is rendered from the last frozen snapshot under the heading
**At close** and reads nothing live. The list (§12) shows one headline reading
per case from the same composition.

*Why.* The live mirror is the property ADR-0107 did not build. It is the
reason the PM opens the case rather than the Front Office (the question's
subject is right there, with the numbers from the moment the question was
asked beside today's), and it is where the program earns its place in the
file without the PM typing. Read-only and unpersisted keeps the analytics
boundary (ADR-0107 §5 "no analytics", ADR-0013/0045) and the record's
integrity: the only persisted form of Where we stand is the frozen one. New
section; confirms ADR-0107 §5's analytics rule.

### 7. System lines, and what Irene may do

The program writes **system lines** to the record: `case_entries` rows of
kind `system_line`, `actor = system`, `actor_user_id` null, with a
machine-readable payload (`event`, the subject, the references) and a
**fixed template text per event**. The sources, exhaustively:

| Event | Written when | Writer (seam) |
|---|---|---|
| `subject_within_ceiling` | the finding's subject falls back within its ceiling (the falling edge) on a case whose finding is `opened_case` | Irene's beat, the internal/signal delta |
| `nav_restated` | a NAV row is written for a linked investment (new or restated as-of) | the NAV write path |
| `press_cluster` | an `rss:cluster:*` rising edge whose matched investment is linked | the RSS delta |
| `ticket_booked` | a ticket with this `case_id` is booked | `TicketService.book` |
| `job_requested` / `job_landed` | a Shirley job for this case is started / its proposal package is stored (§8) | the proposal endpoint; the runner itself is #089 |
| `waiting_lifted` | — this is a milestone (§4), written with `actor = system` when the program lifts the wait; it counts here because the program authored it |

One line per event and subject per case; a restated NAV for the same
investment and as-of is written once. System lines are **mechanical**:
template text from the event's fields, never prose from a model. **Irene
writes no prose on a case** — not a comment, not an assessment, not a
suggestion; her findings reach a case only as the immutable origin embed
and as the mechanical falling-edge line. **Irene opens and closes nothing.**
Shirley's writes are a different matter (§10): they are the PM's request,
not the program's initiative.

This **revises ADR-0107 §3**, "Irene never opens, touches, or comments on a
case", to: *Irene opens and closes nothing and comments never; the program
informs the case mechanically.* Calm by default (ADR-0089) is reinterpreted
for the record: a system line is the Watch Desk's "nothing material" made
visible on the one object that asked — a case with no events has no lines,
and a line never asks for anything.

*Why.* `actor = system` existed with one writer; the walks found four moments
where the program knew something about the case's subject and the case did
not (C-4): the breach cured itself, the fund reported, the press moved, the
ticket booked. Each is a fact, not an opinion, and each belongs in the
record because it changes what the deputy must know. Keeping Irene's prose
out keeps the record free of model output — a line in a decision file must
be reproducible from the event that caused it.

### 8. How the file fills — four channels and proposals

**Rule:** the PM selects what is in the file; the cost of selecting is one
gesture. **Never silent extraction from chat.**

1. **Type in place** — every section is editable text or a list with an
   "add a …" line; autosave per section; the primary path.
2. **Keep from the dock** — every Shirley answer (text, chart) and every
   Planning Desk result carries *Keep →*: one click into the active thread
   (shown as the target in the dock), a second click to choose another
   target (another thread, the file itself, another open case). No comment,
   no dialog (§9).
3. **Shirley on request** — "put the 80 % into the figures", "open a thread
   for the fee question", "note the call: …" — the write tool of §10,
   scoped to the file's structure; it writes only what was asked in this
   turn; every write appears in the dock as a line with **Undo**. Shirley may
   propose, never acts on her own initiative.
4. **The program itself** — Where we stand (§6) and system lines (§7) fill
   without a gesture.

**Proposals.** On "summarise this into the file", when a background job
lands, or on "make this a case" from the dock, Shirley delivers a
**proposal package** — thread conclusions, figures, points, generated
documents, kept artefacts — rendered in the case as a review block and
accepted **item by item**, each editable inline before and after acceptance.
Nothing is written to the working file until an item is accepted; discarded
items leave no trace in the file. A proposal package is persisted on the
case (`case_proposals`) so that a job landing while the PM is away is found
in the case — the case is the inbox; the job runner that fills it is #089.
"Make this a case" drafts the title, the question prose and a first thread
from the conversation; the PM edits and confirms before the case exists.

*Why.* The owner's question after the first mockup was *how the page fills
in a normal workflow*. Extraction from chat was rejected because the file
would stop being the PM's: a case the PM did not compose does not pass the
deputy test (the deputy reads what the PM selected, not what a model
inferred) and does not pass the PM test (a file that fills by itself is not
a desk). Four channels plus proposals keep every write a PM gesture — one
gesture, not a dialog. Supersedes ADR-0107 §5 "consult-and-pin" as the
filling mechanism.

### 9. Keep replaces the pin dialogs; two new kept classes

**Keep** replaces the pin dialogs of ADR-0107 §5/C5/C6 and ADR-0114 §2 (chat
consultation, chat chart, Planning Desk scenario) and the "Pin document"
composer. A kept artefact is a working-file row (`case_kept`) placed in a
thread or at file level; the **curation comment is optional** and editable.
The pin anatomy's gate order (artefact present → case exists → case open)
is kept without the comment gate; transport stays **by reference** — the
client posts the sidecar `artifact_id` (ADR-0114 §2), the server resolves
the spec; a stale artefact renders ADR-0114's calm "no longer available".

Kept classes, with their payload contracts unchanged where they exist:
`document` (ADR-0107) · `consultation` (ADR-0107 C6) · `scenario_snapshot`
(ADR-0107 C5) · `chart_snapshot` (ADR-0114) · **`document_excerpt`** — an
attachment reference, the quoted passage and the page · **`generated_document`**
— a Shirley memo with its sources, delivered as a proposal item, **editable
once accepted**. The payload of every class except `generated_document` is
**immutable once kept**; placement (thread, position, comment, removal) is
mutable. A kept row is removed from the file by `removed_at`, never deleted,
because frozen snapshots reference it (§2). `generated_document` is the one
class whose body is edited in place; its snapshot copy is by value, so the
record keeps each milestone's text.

Pinning to a closed case does not exist (nothing is written to a closed
case). The `pin` and `note` entry kinds are **retired for new writes**;
existing b031 rows of these kinds render as today, read-only, in the record.

*Why.* The pin ceremony was the usage barrier every walk hit (C-10): a
dialog, a picker and a mandatory sentence between an answer and the file.
The curation ADR-0107 wanted still happens — the PM chooses what to keep and
where — it just costs one click. The two new classes are the two things the
walks kept that no class could hold: a passage from a PDF with its page
(LC-1, LC-3) and a memo Shirley wrote (LC-5). Supersedes ADR-0107 §2 "pin
anatomy" and §5 "pin dialog"; extends ADR-0114 §2's class list; ADR-0114 §1
(the sidecar) and §3 (the spec cap) are unchanged.

### 10. Shirley: the file is her brief; read tools; one scoped write tool

- **The brief.** When Shirley is docked beside a case, the stash of
  ADR-0107 C6 / ADR-0133 §6 carries **the working file** — question, Where
  we stand as rendered, threads with status and conclusion, figures, open
  points, the decision draft — composed from the same projections the page
  renders, digest-bounded as today. She reads the file; nothing is said
  twice.
- **Read tools** (`READ_INTERNAL`): `list_cases` (state filter, mine),
  `get_case` (the file as text, any state), `search_cases` over titles,
  threads, figures, conversations, decisions and closing notes —
  **including closed cases**: the archive is the institution's memory
  ("have we had a gated fund before?"). Never over attachment contents (§11).
- **Write tool** (optional for the build; `WRITE_INTERNAL`): `case_add` —
  add a figure, a thread, a point, a conversation, or text to a thread's
  prose, on the case in the brief. It is **additive only**: it never closes,
  records a decision, hands over, sets waiting, removes or edits anything.
  Each write is a line in the dock with Undo; undo deletes the row (or marks
  a kept row removed). The tool is the first member of ADR-0022's
  `WRITE_INTERNAL` class; its gating rules apply unchanged — after a
  `READ_EXTERNAL_UNTRUSTED` tool ran in a turn, `case_add` is locked for the
  rest of that turn, and the lock is visible to her as the tool-result
  error. ADR-0022's follow-up question (a confirmation dialog for
  destructive writes) is answered for this tool: no dialog, because nothing
  it does is destructive and everything it does is undoable in place.
- `prompts/Soul_Shirley.md` is **byte-identical**: tool descriptions carry
  the behaviour (the Soul-hash rule). A one-line addendum, if the build needs
  one, follows the ADR-0137 addendum pattern and is the build's decision.

*Why.* ADR-0107 §5 made Shirley a consultant who is told about the case and
cannot look at it. With the file as her brief and the read tools she sees
what the PM sees, across cases and across time (C-6). The write tool is
where the PM test bites — "put that in the figures" is the natural gesture
on the train — and where the containment matters: scoped to additive writes
on the briefed case, visible, undoable, trust-classed. Supersedes ADR-0107
§5 "zero embedded tools" for Shirley's side; refines C6's brief.

### 11. The DMS boundary holds

ADR-0107 §7 is **confirmed**: size and count caps and the type whitelist
stay configuration; **no folders, no versions, no content search** over
attachments — not in the list search, not in `search_cases`, not in the
brief. The searchable text of a case is what the PM wrote or kept as text
(titles, threads, figures, conversations, decisions, closing notes, an
excerpt's quoted passage); an attachment's contents are never indexed. A
**reading room** — a bounded set of documents Shirley reads on request
within one case, results kept as excerpts and figures — is a Shirley
capability (#084, with #083 document input) outside this ADR; its results
arrive in the file through §8's channels and §9's classes.

*Why.* The boundary was right and is restated so the two new classes cannot
be read as its erosion: an excerpt is a passage the PM (or Shirley on request)
chose and kept; a search over attachment bytes would be a DMS.

### 12. One Section; the case as its sub-surface; A-3 is the acceptance walk

Cases is **one Section** (`web/shell.py` catalogue: one `SectionMeta`, the
landing): the list with the filter **Open · Waiting · Closed · All**, **Mine**
(a filter on the owner, never a data boundary), and search. *Recently closed*
and *Archive* are the Closed filter, newest first, loaded by page. List
columns per the mockup: case · question or task · where we stand (one
headline reading) · threads (count and status) · next due point · owner ·
updated.

The case is the Section's **sub-surface** with the crumb **‹ Cases ›
CASE-nnnn**, rendered inside the shell (no stand-alone page), the outline on
the left, the file in the middle, Shirley dockable on the right (#070's
rule). Milestone actions (Record decision, Wait until…, Hand over to…,
Close case…) and the link/attach actions sit in the case header; everything
else is in place. In-place editing uses **native form controls**, not
`contenteditable` (the mockup's device), so `docs/ux/ui-standards.md` §2.11
holds without exception.

The Section is built on the pf-* vocabulary of M-CS-2 directly; `cases.css`
is replaced by scoped residue, `pf-dc-band-tag` / `pf-dc-res-tag` and the
Cases-scoped `pf-chip` are retired (ui-standards §5 items 5, 14, 15 close),
and **UX strand A-3 becomes this build's acceptance walk** rather than a
separate restyle (roadmap #070 already says so).

*Why.* Three sections of which two were nearly empty was the symptom of a
surface built around the record; one list with a state filter is the shape
of a to-do list the PM keeps. Supersedes ADR-0107 §1 "three surfaces"; the
Section model is ADR-0134 §4's.

### 13. Hand-over is a milestone; owner is the only assignment concept

A case has an **owner** (`cases.owner_user_id`, initially the opener;
`opened_by` stays as history). "Hand over to…" picks a user of the tenant,
writes a `handed_over` milestone (from, to, an optional note) and freezes
the file; the new owner's *Mine* shows it. There is no assignee, no
watcher, no approver; open points carry their own owner (§3) independently.

*Why.* The deputy test needs one gesture that says "it's yours now" and
leaves the file as the predecessor left it — the frozen snapshot at
hand-over is exactly that. Supersedes ADR-0107's non-goal "no case
assignment/delegation mechanics".

### 14. The decision record carries options and the chosen one

The **Decision** section holds options (each a short text; one marked
chosen), the rationale, and decided by/at (`decided_at` may be set to the
committee's date; `recorded_by` / `recorded_at` are the program's). It is a
**draft until recorded**: "Record decision" writes a `decision_record`
milestone with the decision as payload and freezes the file. A case may
record more than one decision over its life (LC-3: coordinate and defer,
then — after waiting — redeem or hold); the section shows the latest
recorded decision and, if present, the current draft. A recorded decision
is immutable; a change of mind is a new decision.

*Why.* ADR-0107 §4 specified "chosen path · alternatives assessed ·
rationale · decided by/at"; the build shipped decision and rationale (C-8).
Restores §4's structure, as a draft the PM composes while the threads are
still open.

## Object model

Migration **b037**. All new tables are tenant-scoped under RLS by
`apply_tenant_rls`, with `tenant_id` denormalised for row-local evaluation
(ADR-0035 §3, the b031 idiom). No DDL here; columns at the level of names
and ownership. State and class vocabularies are TEXT, application-enforced.

**Existing, extended**

- `cases` — unchanged columns; **new**: `state` gains `waiting`;
  `wait_until` (date, null unless waiting); `owner_user_id` (backfilled from
  `opened_by`); `counterparty` (text); `due_on` (date, the case's own due
  date shown in the rail — the mockup's); `framing` (`question` · `task`,
  nullable, presentation only); `updated_at` (the autosave stamp).
  `description` is reinterpreted, not renamed: it holds **the question's (or
  task's) prose**; manual b031 cases keep their description as that prose;
  cases from findings start with it empty. The permitted mutations of a
  `cases` row are now: the working-file columns above while open or
  waiting; the state transitions of §4; the owner at hand-over; the close
  trio once.
- `case_entries` — **the record**; append-only, unchanged in shape. The
  `kind` vocabulary becomes: `opened` · `handed_over` · `waiting_set` ·
  `waiting_lifted` · `decision_record` · `closed` · `frozen` · `system_line`,
  with `note` and `pin` retired for new writes (legacy rows readable). A
  `frozen` entry carries `payload.milestone_entry_id` and the whole-file
  snapshot (§2); the paired rows are written in one transaction and
  rendered as one record row. `actor` stays `pm` · `shirley` · `system`;
  new record rows are written by `pm` (milestones) or `system` (system
  lines, a program-lifted wait). `shirley` remains in the vocabulary for
  legacy rows and is not written by this build: Shirley writes the working
  file (§10), never the record.
- `case_attachments` — unchanged (bytes, caps, no dedup). Addressed through
  `case_kept` rows of class `document` / `document_excerpt`; an attachment
  with no kept row is an upload in progress.

**New, working file (mutable)**

- `case_threads` — `case_id`, `position`, `title`, `status` (`open` ·
  `confirmed` · `parked` · `refuted`), `prose`, `conclusion`, `created_at`,
  `concluded_at`, `removed_at` (soft removal: kept rows reference the
  thread).
- `case_kept` — `case_id`, `thread_id` (null = file level), `position`,
  `artifact` (the six classes of §9), `caption`, `comment` (optional),
  `payload` (JSONB, the class contract — immutable except for
  `generated_document`), `payload_sha256`, `attachment_id` (null unless
  `document` / `document_excerpt`), `kept_by` (the user who kept or accepted
  it), `via` (`keep` · `shirley` · `proposal` — the channel of §8),
  `kept_at`, `removed_at`.
- `case_figures` — `case_id`, `position`, `label`, `source` (`book` ·
  `typed` · `document` · `kept` · `conversation` · `external`), `value_text`
  (null for `book` rows, which are read live from `source_ref` and written
  only into snapshots), `source_ref` (JSONB: the read key for a book row —
  investment and metric; attachment and page for a document; a kept id; a
  conversation id; free text for external), `as_of` (text as displayed),
  `note`. Hard-deletable (snapshots embed figures by value). **No
  formulas**: a figure is a value and its provenance, never an expression.
- `case_conversations` — `case_id`, `position`, `held_on`, `channel` (call
  · meeting · other, text), `with_whom`, `what_was_said`,
  `what_was_promised`, `duration_minutes` (optional). Hard-deletable.
- `case_points` — `case_id`, `position`, `text`, `owner_user_id`, `due_on`,
  `done_at`, `ticket_id` (the ticket created from this point; the ticket's
  own `case_id` is the back-link). Hard-deletable while no ticket is linked.
- `case_decisions` — `case_id`, `options` (JSONB list of `{key, text}`),
  `chosen_key`, `rationale`, `decided_by`, `decided_at`, `recorded_by`,
  `recorded_at` (null = the draft; at most one draft per case),
  `record_entry_id` (the `decision_record` entry once recorded). Recorded
  rows are immutable.
- `case_investments` — `case_id`, `investment_id`, `linked_by`, `linked_at`;
  unique per pair.
- `case_proposals` — `case_id`, `requested_by`, `requested_at`,
  `request_text`, `landed_at`, `items` (JSONB list of proposal items with
  per-item `status`: pending · accepted · discarded and the created row's
  id on acceptance), `resolved_at`. The landing contract of #089; in v1
  filled synchronously by the dock's "summarise into the file" and "make
  this a case".

**Not changed**: `irene_finding` (ADR-0085), `trade_tickets` (ADR-0128;
`case_id` already there), the Journal (a projection, ADR-0133 §5), the chat
sidecar (ADR-0114 §1), the tool registry's class mechanics (ADR-0022).

*Why separate tables and not one `case_sections` JSONB column.* Figures,
points and threads are queried individually — the list's "next due point",
search over thread titles, Shirley's `search_cases`, the ticket back-link
from a point — and edited individually by autosave; a JSONB blob would make
every autosave a whole-file write and every query a JSON scan. The frozen
snapshot is the one place the file is a blob, and that is its purpose.

*Why a `frozen` entry beside the milestone entry and not a `snapshot`
field on it.* Milestone entries stay small and readable — in the Journal
projection, in the brief's digest, in the record's rendering — and the
snapshot payload has one shape regardless of which milestone caused it. The
pair is written atomically; rendering joins them by `milestone_entry_id`.

## Alternatives considered

- **Remove Cases altogether** — the consultant's question, and path (a) of
  the owner's own framing. Rejected on the evidence of the walks: all five
  live cases are work the PM does today, outside the program, with the
  program's own data; Transactions answers only the half that becomes an
  order; the Watch Desk cannot hold a question for a week. The record half
  (origin, immutable entries, Journal) is built and right. The cost of
  dismantling is not lower than the cost of finishing.
- **Fold Cases into the Watch Desk as a fourth Section.** Rejected: the
  Watch Desk is calm by default and system-born (ADR-0089, ADR-0133); a case
  is PM-born, lives for weeks and holds typed work. The two moods do not
  share a surface; the ADR-0122 sidebar order narrates the hand-over
  (question → case → transaction) and keeps it.
- **A case-type model** (question · task · watch with templates and per-type
  outlines). Rejected (§3): the walks share one structure and differ in
  which sections fill; types decide ahead of the work. A presentation flag
  carries the only visible difference.
- **Silent extraction from chat** (the file fills from the conversation
  without a gesture). Rejected (§8): the file stops being the PM's and fails
  both tests; proposals accepted item by item keep the convenience and the
  authorship.
- **Per-section freeze deltas instead of a whole-file snapshot.** Rejected
  (§2): a delta record must be replayed to read, and a reviewer reads a
  state, not a diff; whole-file snapshots are self-contained and cheap
  (prose and figures, artefacts by immutable reference). The record grows
  at milestones only, and milestones are rare.
- **A separate reviewer surface now** (period filter, export, read-only
  mode). Deferred: the reviewer is a beneficiary; the Journal serves the
  period view; export is #090's path (#001). Building a reviewer surface
  before the PM surface exists would repeat ADR-0107's order of priorities.
- **Milestone entries carrying the snapshot inline.** Rejected (Object
  model): one snapshot shape, small milestone rows.
- **Status quo** (ADR-0107 as built, polished under A-3). Rejected: a
  restyle does not add the live mirror or the linkage, and the pin ceremony
  is the barrier, not its styling.

## Consequences

### Positive

- The two tests become answerable: the PM's working material has a home with
  the book's figures beside it; the deputy finds one outline, provenance on
  every number, a status on every thread, a frozen state at every milestone.
- The record keeps every guarantee it has (append-only, immutable, Journal
  projection unchanged) and gains what it lacked: the file as it stood when
  the decision was taken, and the facts the program knew about the subject.
- Linkage is machine-readable in both directions: finding → case →
  ticket → bookings (ADR-0128's chain) now reads back on the case.
- Shirley becomes useful on a case: she sees the file, searches the archive,
  and writes on request under a declared trust class — the first
  `WRITE_INTERNAL` member of ADR-0022, contained as that ADR requires.
- Three list sections and a stand-alone page become one Section on the pf
  vocabulary; `pf-dc-*` leaves the tree; ui-standards §5 loses three accepted
  deviations.

### Negative / accepted

- **Two layers to keep consistent.** Every milestone must write the
  milestone entry, the snapshot and the state change atomically; the
  working-file tables of a closed case must be read-only by route and by
  test. Accepted: this is the price of a mutable desk over an immutable
  record, and it is one transaction per milestone.
- **The first write tool.** `case_add` is additive and undoable, but it is
  the first time a model writes application state; the trust class, the
  turn lock and the dock's undo line are the containment, and the tool's
  scope is this ADR's boundary — every widening is a successor ADR.
- **Where we stand's read cost.** A case page reads the Watch Desk
  resolution path, n investments, the tickets and the FX repository on each
  render; the list reads one headline per row. Accepted: these are the same
  reads the Watch Desk and Front Office already do; the composition is built
  once per request and never cached across requests (it must be live).
- **The record grows at milestones.** A snapshot is the file's prose,
  figures, points and references — kilobytes, not the chart specs. Accepted;
  milestones are rare by construction.
- **Legacy cases.** Cases opened under ADR-0107 get an empty working file and
  a record of legacy kinds; nothing is migrated into the new tables.
  Accepted: there are few, and the record is the part that matters.

### Risks — the scope-creep points, named

The pressure will arrive exactly here, and this ADR is the reference for
declining each until its own ADR exists:

- **the reading room** (#084) — "let Shirley read the attachments";
- **formulas in Figures** — "let me compute the fee drag here";
- **the job runner** (#089) — "let the AGM pack run in the background" (the
  landing place is built; the runner is not);
- **case templates** — "a type for due diligence with its own sections";
- **a reviewer export** (#090 / #001) — "give me the file as a PDF".

### Neutral / follow-ups

- The glossary row **Case** (`docs/glossary.md`) is replaced with the build
  (ADR-0134 §2: a term enters with the build that lands it).
- The build decides the autosave transport (one endpoint per section,
  last-write-wins on `updated_at`) and the test seam (the section endpoints,
  ASGI tests as today); neither is a decision of record.
- When #089 lands, `job_requested` gains its emitter; `case_proposals` and
  the `job_*` lines are the contract it fills, defined here so the runner
  needs no Cases change.
- ADR-0022's open question on a confirmation dialog for destructive
  `WRITE_INTERNAL` tools stays open for a future destructive tool; it is
  answered here only for `case_add`.

## Non-goals (binding)

- No DMS semantics: no folders, no versions, no content search over
  attachments — in no search, no tool, no brief (§11).
- No formulas, no computation in Figures; no analytics in Cases; nothing new
  in `services/analytics`; Where we stand composes existing reads only (§6).
- No scenario re-hydration, no snapshot editing (ADR-0107 §6 confirmed).
- No provider UI, no engagement trace on a case (ADR-0107 §Context; ADR-0129
  is its own surface).
- No Irene prose on a case; no case opened or closed by the program (§7).
- No job runner; `case_proposals` is the landing place, #089 is the runner.
- No case kinds as a type; one presentation flag only (§3).
- No reviewer export, no reviewer mode, no period view in Cases (the Journal
  serves the reviewer).
- No assignee, watcher or approver beyond the owner (§13); no four-eyes on
  a case (tickets have theirs, ADR-0128).
- No configurable wake events (§4).
- The mockup's Round-2 affordances — "Export the pack (PDF)", the header's
  "Shirley working on n" counter, a running job's progress — are **not
  rendered in v1**; nothing disabled or promised ships (ADR-0107's release
  rule holds).

## Commissions (recorded here, not designed here)

- **Roadmap.** #078 is this ADR's item (Active); the workflow-round
  candidates G-1…G-15 of the register are **#079–#093** as filed by docs
  prompt P-CS-D1 (2026-11-02; next free #094) — they are referenced, not
  re-commissioned. The build kickoff (KO-CS-B, written by Mission Control
  from this ADR's closing report) sequences the strands.
- **Glossary.** A successor row **Case** — the definition of §1, the two
  layers, the three states, the outline, the kept classes, "one Section" —
  written **with the build**, replacing the ADR-0107 row (ADR-0134 §2).
- **`docs/ux/ui-standards.md`.** §5 items 5, 14 and 15 close with the
  Section build; §8's Cases row is ticked by the acceptance walk; §4's
  `pf-chip` homonym note is removed when the Cases-scoped rules are gone.
- **UX strand A-3** is this build's acceptance walk (roadmap #070 already
  reads so); the owner answers the two tests in writing after it.
- **`AGENTS.md` and `docs/architecture.md`** paragraphs on Cases are updated
  **with the build, not before** (ADR-0107 and ADR-0128 §7 precedent).
- **ADR index.** The operator files this ADR, assigns its number, and adds
  the README row; the ADR-0107, 0114 and 0120 rows are not edited.
- **`prompts/Soul_Shirley.md`** is not touched by this ADR or its build; tool
  descriptions carry the behaviour.

## Implementation Notes (pointers for the build kickoff; not prompts)

- Models: `core/models/case.py` (extend `Case`, `CaseEntry` docstrings; new
  working-file models), `core/models/investment.py` (`manager_name` read
  only), `core/models/trade_ticket.py` (`case_id`, unchanged).
- Migration: `db/migrations/versions/…_b037_…` after b036; b035 stays
  reserved.
- Repositories: `core/repositories/case_repository.py` (states, owner,
  milestones with freeze, `system_line` writer, search over the working
  file), `case_attachment_repository.py` (unchanged), new working-file
  repositories.
- Composition: a read-only service for Where we stand over
  `services/watch_desk/family_status.py`, the overlay, the investment reads
  behind `get_investment_data`, the FX repository and the ticket reads;
  nothing under `services/analytics`.
- System-line writers: Irene's beat / delta paths (`services/irene/`), the
  NAV write path, `services/irene/rss_delta.py`,
  `services/transactions/ticket_service.py::book`, the proposal endpoint;
  the waiting wake-up in `services/scheduler/tick_runner.py` (ADR-0117).
- Web: `web/routes/cases.py` (one Section, the sub-surface, section autosave
  endpoints, milestone actions, Keep targets, proposals), `web/routes/chat.py`
  (brief = the file; Keep on bubbles replaces `PIN_CONSULTATION_URL` /
  `PIN_CHART_URL`), `web/routes/planning_desk.py` (Keep replaces
  `PIN_SCENARIO_URL`), `web/routes/watch_desk.py` (open-case composition
  unchanged; falling-edge line), `web/routes/transactions.py` (case picker
  unchanged), `web/shell.py` (one `SectionMeta`), templates under
  `web/templates/_partials/cases_*.html` rebuilt on the pf vocabulary,
  `cases_detail.html` retired, `cases.css` replaced by scoped residue.
- Shirley tools: new module under `services/tools/` registering
  `list_cases`, `get_case`, `search_cases` as `READ_INTERNAL` and `case_add`
  as `WRITE_INTERNAL` (`services/tool_classes.py`, `services/tool_registry.py`
  unchanged).
- Tests: `tests/web/test_cases_area.py`, `tests/repositories/test_case_*.py`,
  `tests/regression/test_b031_case_workflow_roundtrip.py` extended to b037
  (round-trip of the new tables, the freeze pair, legacy kinds readable),
  tool tests for the four tools incl. the ADR-0022 turn lock,
  `tests/web/test_pf_components.py` pins for "no `pf-dc-` under Cases".
- Configuration: `core/config.py` attachment caps unchanged.

## Compliance & Audit Relevance

- **ISO 25010:** Maintainability (one outline, one Section, one snapshot
  shape), Reliability (atomic milestone + freeze; deterministic rendering of
  closed cases), Security (the first `WRITE_INTERNAL` tool under ADR-0022's
  gating; additive, undoable, scoped), Usability (the two tests).
- **Regulatory references:** BAIT/VAIT — documented decision files with
  immutable records and reproducible decision-time state; containment of
  LLM-driven writes (BAIT AT 7.2, per ADR-0022).
- **Audit evidence:** the `case_entries` rows of a case (milestones, `frozen`
  snapshots, system lines) and their tests; the `case_add` registration with
  its class and the turn-lock test; the read-only guard on closed cases'
  working-file tables; this ADR and its index row.

## References

- ADR-0107 (Cases as built; the clauses this ADR supersedes, refines and
  confirms are named per section), ADR-0114 (chart snapshots; sidecar and
  cap unchanged, class list extended), ADR-0120 (band gate, unchanged).
- ADR-0085, 0088, 0089, 0116, 0133 (findings, synthesis, calm by default,
  families, briefing and Journal); ADR-0104 (stateless scenarios); ADR-0128
  (tickets, `case_id`, the ninth Area); ADR-0050 (ephemeral chat);
  ADR-0022/0023 (tool classes, Fetcher); ADR-0117 (tick runner); ADR-0134
  (Section as one view; precedence); ADR-0035 (RLS idiom); ADR-0122
  (sidebar order); ADR-0129 (provider channel — b035 reservation).
- `docs/concepts/cases-rework-concept.md` — the concept record, decisions
  C-D-1…C-D-14, §10 open points (settled here: Threads; whole-file freeze;
  wake events fixed; `description` as the prose; section autosave as the
  test seam; `generated_document` in `case_kept.payload`; native controls
  for a11y).
- Track artefacts outside the tree: `Workflow-gaps_register_2026-11-02.md`
  (LC-1…LC-5, G-1…G-15, C-1…C-14), `M-CS-2_cases-mockup.html`, the
  implementation plan.
- Roadmap #078 (this ADR), #079–#093 (the workflow round), #070 (A-3),
  #071 (the turn lock, prerequisite of #089).

---

## Revision History

| Date       | Author                     | Change                                   |
|------------|----------------------------|------------------------------------------|
| 2026-11-02 | DC-CS-A (daughter chat)    | Initial draft (Proposed) from the concept record and KO-CS-A |
| 2026-11-02 | PortfoliFLOW project owner | Accepted; number assigned; index row added |
