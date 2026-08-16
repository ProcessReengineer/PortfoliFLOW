# ADR-0107: Case Workflow — the Cases Area

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Case Workflow (successor of the "Execution
  Network" concept kernel; new roadmap entry to be created on filing)
- **Tags:** cases, decision-console, irene, shirley, planning-desk, journal,
  workflow, audit-trail, agpl-release-scope
- **Refines / clarifies:** ADR-0085 (findings immutability and resolution
  vocabulary — this ADR adds one resolution member, `opened_case`; ADR-0085
  remains Accepted and unedited), ADR-0106 (the "Open case →" affordance ships
  disabled there; this ADR is the workstream that arms it).
- **Honours:** ADR-0088 (synthesis contract — untouched), ADR-0089 (calm by
  default — cases are opened by the PM, never by Irene), ADR-0104 (scenario
  overlays are stateless parameter sets — snapshots freeze data, they do not
  create saved scenarios), ADR-0013/0045 (analytics purity — the Cases area
  contains no analytics), ADR-0008 (English-only codebase).
- **Binding mock:** `docs/handover/cases-area-mockup-v2.html` (concept mockup
  v2 from the concept chat; illustrative data — where mock and code disagree
  during implementation, the decisions in this ADR win, then the code).

---

## Context

The Decision Console refresh ("One Glass", B-final-v3, closed 2026-07-20)
turned the Console into a trustworthy *monitoring* surface, but the word
*Decision* in its name was not yet earned: the Console observes and reports; it
does not help decide, and nothing connects a surfaced finding to what the
manager subsequently did about it. DC3 shipped an "Open case →" affordance as a
deliberately disabled preview; its target did not exist.

The original "Execution Network" brainstorm imagined that target as a case
workspace *plus* a centrally hosted provider directory with structured
hand-offs to execution partners. The concept discussion deliberately cut that
scope: the provider directory presupposes an adopted product and a defensible
listing base, it sits closest to the regulatory red line (PortfoliFLOW must
remain a software provider — a venue for the PM's own decisions, never a broker
or advisor), and the case workflow is complete and valuable without it. The
directory and any engagement protocol are **out of scope of this ADR entirely**
and, unlike in earlier drafts, leave **no visible trace in the shipped UI** —
no ghost buttons, no "coming soon". This is a deliberate consequence of the
release context below.

**Release context.** This is the last feature set implemented before the AGPL
release is prepared (followed by a dedicated UI-polish pass). Scope and style
must match a public, professional product: the shipped surface is complete in
itself, contains nothing disabled or promised, and adds no speculative
machinery.

**What the feature is.** A Case is an open question about the portfolio,
worked to a documented close. Cases give the tenant two things at once: a
self-filling to-do list (open cases, whoever opened them) and an auditable log
of resolution initiatives (closed cases with mandatory closing notes, readable
by a reviewer, auditor, or supervisor). The institutional value is the
**documented decision file** — finding → assessment with evidence → (optional)
decision of record → close — which is exactly the platform's moat: domain
expertise and institutional knowledge, not code.

**Verified constraints from the codebase** (fresh Repomix, 2026-07-20):

- The finding resolution vocabulary (`open` / `acted` / `dismissed` /
  `acknowledged`) is enforced in application code, not as a SQL enum — adding a
  member is a code + test change, no migration of an enum type.
- Irene's watch-subject families are `saa:*`, `anlv:*`, `rss:cluster:*`. No
  price-movement watcher exists; a `price:*` family is a separate Console-side
  roadmap candidate (see §Commissions). The case workflow is source-agnostic
  and needs no knowledge of subject families.
- Planning-Desk scenarios are **stateless** (ADR-0104): no persisted scenario
  object exists. A case therefore cannot *link* to a scenario; it can only
  freeze a **snapshot** (this ADR, Decision 6).
- The scenario results region already renders a structured view model
  (parameter strip; four KPI base/scenario/delta pairs; per-family headroom
  rows; end-of-horizon NAV and total return with deltas). The snapshot
  serialises exactly this — nothing new is computed.
- The sidebar's area list is hardcoded in `_AREAS` order by design; adding an
  area is an explicit, recorded ordering decision (Decision 1).

## Decision

### 1. A new top-level area: **Cases**

Cases is the eighth area, placed **between Decision Console and Planning
Desk** in `_AREAS` and the sidebar: it consumes the Console's findings and
feeds on the Planning Desk's scenarios, so it sits between them in the
manager's mental flow. Slug `cases`, route `/cases`.

The area has three surfaces, top to bottom (see the binding mock):

- **Open cases** — the to-do list. All open cases of the tenant, with owner
  shown per row and a "Mine" filter (a filter on `opened_by`, never a data
  boundary). A "New case" action creates a manual case (Decision 3).
- **Case detail** — the file: origin (if any), the append-only timeline, a
  status/linked-objects/actions rail.
- **Recently closed** — the last **5** closed cases, each row showing owner, a
  closing-note excerpt, and the Journal reference. Below it, a collapsed
  archive search over **titles and closing notes only** (never attachment
  contents — the DMS boundary, Decision 7, holds here too). This is the
  reviewer's surface.

### 2. Object model and persistence

Two tables, tenant-scoped under RLS like everything else:

- **`case`** — `id`, `tenant_id`, `case_number` (tenant-sequential display
  number), `title`, `state` (`open` / `closed`, TEXT per the codebase's
  status convention, enforced in application code), `opened_by`, `opened_at`,
  `closed_by`, `closed_at`, `closing_note`, and an **optional**
  `finding_id` referencing the originating finding. The case *references* the
  finding; it never mutates it (ADR-0085). Mutations to a case row are
  limited to the close transition (state, closed_*, closing_note).
- **`case_entry`** — the timeline. `id`, `tenant_id`, `case_id`, `kind`,
  `actor` (`pm` / `shirley` / `system` plus the user id where applicable),
  `created_at`, and a `payload` JSONB opaque to persistence (mirroring the
  finding-payload idiom). **Append-only: entries are never updated or
  deleted.** A new situation is a new entry.

Entry `kind` vocabulary (TEXT, application-enforced):
`opened` · `note` · `pin` · `decision_record` · `closed`.

Pins carry an artifact of one of three classes in their payload:
`document` (external file), `consultation` (pinned Shirley excerpt),
`scenario_snapshot` (Decision 6). Every pin consists of **a mandatory short
user comment (the curation — why is this decision-relevant?) plus the
artifact**.

File attachments reuse the existing upload infrastructure where suitable
(pre-flight verification item in the kickoff); they are addressed only through
their pin entry.

### 3. Two entry points, one object

- **From a finding:** the Console card's "Open case →" (armed by this
  workstream) creates a case pre-filled from the finding (title from the
  trigger, origin embedded read-only with the materiality *at case opening*).
  Opening a case resolves the finding as **`opened_case`** — a new member of
  the ADR-0085 resolution vocabulary recorded by this ADR. The card leaves the
  Briefing feed; the Journal shows the hand-over; the case carries the live
  state from then on.
- **Manually:** "New case" with title and description. No finding reference.
  This serves decision types that never pass through Irene (e.g. board-meeting
  preparation from received fund documents).

Cases are opened by people. **Irene never opens, touches, or comments on a
case** — calm-by-default (ADR-0089) extends unchanged into this area.

### 4. Two states, one mandatory closing note, one optional decision record

The lifecycle is **Open → Closed**. Nothing else. "Assessment", "decision",
and "action" are timeline *activities*, not states.

- **Closing note** — mandatory, exactly one, written at close. It answers
  *"why is this case done?"* and may be as short as "prices recovered". It is
  rendered in the `closed` entry, excerpted in the Recently-closed row, and
  feeds the Journal entry created on close (which references the case and, if
  present, the originating finding).
- **Decision record** — optional, a timeline entry (`decision_record`),
  structured: chosen path · alternatives assessed · rationale · decided by/at.
  It answers *"what did we choose among alternatives, and why?"* and exists
  for cases that deserve board-grade deliberation. A case the world resolved
  closes with a note and no record; a decision can precede the close by days
  of implementation work.

Closed cases are read-only in their entirety.

### 5. The tooling principle: consult-and-pin, zero embedded tools

**The Cases area contains no analytical, generative, or document-management
capability of its own.** Its actions fall into exactly two classes:

- **In-case composers** (plain forms writing timeline entries): Add note ·
  Pin document · Record decision · Close case.
- **Navigation with context** (to where the tool already lives): Consult
  Shirley · Capture scenario.

The rule in one sentence: *artifacts are created where their tool lives;
pinning brings them into the file.* Conversations with Shirley remain
ephemeral, like every Shirley conversation; only what the PM deliberately pins
— always with the curation comment — becomes part of the file. A full chat
transcript is not an audit artifact; the curated excerpt is.

- **Consult Shirley** opens a Shirley conversation whose context carries the
  **case brief** automatically: finding payload with materiality (if any),
  timeline summary, linked investments. Shirley's side gains exactly one new
  affordance: a pin action on assistant messages, opening the standard pin
  dialog (open-case picker, comment field).
- **Capture scenario** navigates to the Planning Desk's Scenario Analysis lens
  with a slim "capturing for CASE-XXXX" context marker. The Planning Desk
  gains exactly one new affordance: a **"Pin to case…"** button on the
  scenario results region, opening the same pin dialog (case pre-selected when
  arrived with context).

### 6. Scenario snapshots: frozen evidence, never saved scenarios

Because scenarios are stateless (ADR-0104), a snapshot **freezes exactly the
data the results region already computes and renders**: the parameter set, the
KPI base/scenario/delta pairs, the per-family headroom rows, and the
end-of-horizon NAV/total-return pair. Hard rule: **nothing the Planning Desk
did not already compute and display; zero new analytics.** Charts (Plotly
figure dicts) are deliberately **not** captured in v1: they are heavyweight,
would need their own read-only render path, and the figures carry the full
evidentiary content.

**No re-hydration.** A snapshot cannot be "reopened in the Planning Desk".
Re-hydration would make snapshots de-facto saved scenarios and smuggle in a
scenario manager (naming, versioning, comparison) through the back door,
contradicting ADR-0104. The snapshot is evidence, not a save-state; the
parameters are legible in it, and re-entering them is the intended path.

No pinning to closed cases. No editing a snapshot — a new situation is a new
pin.

### 7. The DMS boundary

External attachments are bounded so the case file cannot drift into a
document-management system in disguise:

- Size cap per file (default **10 MB**), count cap per case (default **20**),
  type whitelist (**PDF, PNG/JPG, XLSX/CSV**). Defaults are configuration,
  operator-adjustable; the *existence* of the caps is the decision.
- **No** folders, **no** versioning, **no** full-text search over attachment
  contents (the archive search covers titles and closing notes only).
  Attachments exist solely as pin entries of one case; a document needed in
  two cases is pinned twice.

The file *references evidence*; it does not *manage documents*.

### 8. Blast radius (exhaustive)

1. **Cases area** — new: models, migration, repositories, routes, templates,
   CSS, tests.
2. **Planning Desk** — one button ("Pin to case…") + one dialog + the snapshot
   serialisation of the existing view model. No analytics change.
3. **Shirley** — case-brief context injection + one pin affordance on
   assistant messages.
4. **Decision Console** — arms the existing "Open case →" button; adds
   `opened_case` to the resolution vocabulary (code + tests); Journal entries
   gain the case reference.

Nothing else is touched — in particular: not the analytics layer, not the
scenario architecture, not Irene's beat, not the limits engine.

## Consequences

**Positive.** The Console's "Decision" is earned: findings connect to
documented outcomes. The tenant gets a shared to-do list and an auditable
resolution log with zero extra ceremony (one mandatory free-text note per
case). The append-only timeline plus the immutable finding reference make
every closed case a self-contained audit record. The consult-and-pin funnel
gives one mechanism for all evidence classes, and the two-state model keeps
the workflow machine near-invisible. The provider/engagement concept retains a
clean seam (an engagement would be a future timeline activity after a
decision) without any present-day cost or UI residue.

**Negative / accepted costs.** Pinning requires deliberate user action — a
manager who consults Shirley and pins nothing has an empty file; this is by
design (curation is the feature) but shifts documentation discipline onto the
user. Re-entering scenario parameters by hand (no re-hydration) is deliberate
friction. The tenant-sequential `case_number` needs a race-safe allocation.
The Shirley brief injection adds a second structured-context path into the
assistant that must be kept in step with the case model.

**Risks.** Scope creep pressure will arrive exactly at the excluded points:
re-hydration, attachment search, workflow states, provider hooks. This ADR is
the reference for declining them; each would need a successor ADR, not a quiet
extension.

## Non-goals (binding)

- No provider directory, no engagement protocol, no order routing, no
  execution by PortfoliFLOW, and **no UI trace** of any of these.
- No scenario manager, no snapshot re-hydration, no snapshot editing.
- No document-management semantics (folders, versions, content search).
- No workflow states beyond Open/Closed; no per-entry editing or deletion.
- No auto-opened cases; no Irene involvement of any kind.
- No case assignment/delegation mechanics in v1 (`opened_by` is the owner;
  reassignment is a future concern if ever needed).

## Commissions (recorded here, not designed here)

- **Roadmap entry: Case Workflow** — this ADR's implementation (the kickoff
  handover defines the sub-strands).
- **Roadmap candidate: `price:*` watch-subject family** for Irene (definable
  price-movement thresholds on instrument prices), so market-movement cases
  become finding-originated. Console-side; explicitly not part of this
  feature set.
- **Roadmap update:** the "Execution Network" concept entry is superseded by
  this ADR for its case-workflow half; the provider-directory half remains a
  dormant concept, revisited only after AGPL release and demonstrated
  adoption.

## References

- Concept chat "Execution Network & Case Workflow" (July 2026) — handover and
  discussion; mockups v1/v2.
- Decision Console refresh closure note (B-final-v3, 2026-07-20) — the
  disabled "Open case →" preview this ADR arms.
- ADR-0085, ADR-0088, ADR-0089, ADR-0104, ADR-0106 — honoured as listed above.
