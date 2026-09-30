# ADR-0134: AGENTS.md as the Agent Instruction File, a Standalone Canonical Glossary, and Section as One View

- **Status:** Accepted (2026-09-30)
- **Date:** 2026-09-30
- **Deciders:** PortfoliFLOW project owner
- **Supersedes / amends:** **amends ADR-0015** — the project instruction file
  is `AGENTS.md`, not `CLAUDE.md`; the workflow ADR-0015 describes is
  otherwise unchanged. **Supersedes ADR-0084 in part** — the location of the
  canonical glossary (its Decision's opening sentence and Implementation
  Notes), its item 5 (source-of-truth precedence), and the Section sentence
  of its item 1; the rest of ADR-0084 stands. **Supersedes ADR-0058 in
  part** — the *Per-area surfaces* and *Section indicator* paragraphs of its
  Decision and the anchor-scroll behaviour of section navigation in its
  *URL structure* and *Command palette* paragraphs. ADR-0015, ADR-0058 and
  ADR-0084 remain immutable and are not edited.
- **Tags:** process, glossary, documentation, ai-workflow, navigation, ui

---

## Context

PortfoliFLOW is built by one developer with AI coding agents (ADR-0015) and
is about to be reviewed by external software firms. Three weaknesses in its
documentation set make the project's own rules harder to trust than the code
they describe.

**The instruction file is named for one vendor and has drifted.** ADR-0015
fixed `CLAUDE.md` as the file Claude Code loads at the start of every
session, while noting that the conventions in it are vendor-neutral. Other
coding agents read `AGENTS.md`. Since version 2.1.277 (2026-09-18) Claude
Code also reads `AGENTS.md` when no `CLAUDE.md` is present. Checked against
the tree, `CLAUDE.md` names a migration head that is no longer the head, a
project status table and an ADR topic table that stopped being
maintained, a
template path that does not exist, and dependency rules that are looser in
one place and stricter in another than the regression guards that enforce
them.

**Terminology lives in three places, and two claim to be canonical.**
ADR-0084 made the table in `CLAUDE.md` the canonical glossary, mirrored in
`docs/architecture.md`, and rejected a standalone `docs/glossary.md` because
it would add "a third place terminology can live and drift". In practice
the mirror drifted anyway: `CLAUDE.md` carries 49 terms, the architecture
document 34, and 19 of the shared entries are worded differently. The
architecture document calls itself the canonical source for terminology,
contradicting ADR-0084. `docs/translation-glossary.md` is a third, older
table of German-to-English equivalents.

**The Section term no longer describes the product.** ADR-0058 introduced
the web information architecture as long-scroll Area pages whose sections
are reached by anchor-scrolling, with a dot indicator on the right edge.
ADR-0084 item 1 defined Section accordingly. The UX overhaul replaced that
model: on 2026-09-21 the owner chose *one question per screen — one section
per view*, and strand A-0 of the overhaul shipped it. Today an Area shows
exactly one Section at a time, the URL fragment selects it, hidden sections
never load, and the sidebar lists the active Area's sections as a second
level in place of the indicator. The rules live in
`docs/ux/ui-standards.md` §2.1; no ADR records the change. Under the
"ADR wins" doctrine, a corrected glossary entry would lose to ADR-0058 and
ADR-0084 the moment it is written.

This decision is audit-relevant: a reviewer must be able to tell, for any
two documents that disagree, which one is right.

## Decision

1. **Instruction file.** The project instruction file for coding agents is
   `AGENTS.md` at the repository root. The repository contains no
   `CLAUDE.md` and no `CLAUDE.local.md` — no stub importing `AGENTS.md`, no
   symlink. `AGENTS.md` is written for coding agents in general; its only
   tool-specific content is the statement that Claude Code reads it when no
   `CLAUDE.md` exists, and that none may be added. This amends ADR-0015's
   naming of the instruction file; the architecture review protocol
   ADR-0015 places "in `CLAUDE.md`" is documented in
   `docs/architecture.md` (§Architecture review protocol).

2. **One canonical glossary.** `docs/glossary.md` is the only glossary. It
   holds the union of the two former tables, de-duplicated and checked
   against the code; the project-name spellings; the common-mistakes table;
   and the German-to-English domain equivalents of the former
   `docs/translation-glossary.md`, which is retired. `AGENTS.md` and
   `docs/architecture.md` point to it and carry no copy of any entry. A
   term enters the glossary only for something the system has built; a
   term decided by an ADR whose build has not landed enters with that
   build.

3. **Precedence.** Where documents disagree:
   - an accepted ADR wins over every other document;
   - `AGENTS.md` wins on rules (what code and agents must and must not do);
   - `docs/glossary.md` is canonical for terms;
   - `docs/architecture.md` is the narrative and yields to the other three.

   A disagreement is fixed in the losing document. Where the ADR is the
   stale one, the fix is a successor or annex ADR, never an edit.

4. **Section is one view.** A Section is one view within an Area. An Area
   shows exactly one Section at a time; the URL fragment selects it
   (`/front-office#charts`); a Section that is not shown is not loaded. The
   catalogue `_SECTIONS_BY_AREA` in `web/shell.py` defines which Sections
   an Area has, their order, the landing Section, and the role gate; the
   Area body partials must match it. This records the navigation model the
   owner decided on 2026-09-21 and strand A-0 of the UX overhaul shipped;
   `docs/ux/ui-standards.md` §2.1 remains the normative detail. It
   supersedes the Section sentence of ADR-0084 item 1 (Repository is
   unchanged) and the ADR-0058 paragraphs named above. Nothing else in
   ADR-0058 is re-decided here.

## Rationale

- **One home per kind of truth.** Rules, terms, narrative and decisions
  each get one document and a stated rank. ADR-0084's objection to a
  standalone glossary was a third drifting copy; after this decision there
  is exactly one copy instead of the current two.
- **The glossary serves more than one reader.** Agents, human contributors
  and reviewers all need the terms. A file of its own can be read and cited
  without the rest of an agent instruction file, and keeps `AGENTS.md`
  short enough to hold rules only.
- **Vendor-neutral by name as well as content.** ADR-0015 already held the
  conventions vendor-neutral; naming the file for the shared convention
  lets any agent pick it up and removes the need for parallel files.
- **The "ADR wins" rule needs an ADR that says the right thing.** ADR-0084
  itself argues that a glossary is only worth keeping if it describes the
  system as built. Recording the Section change in an ADR is what lets the
  corrected definition stand under the precedence rule of item 3.

## Alternatives Considered

- **Keep `CLAUDE.md` and add `AGENTS.md` beside it.** Rejected: two
  instruction files drift exactly as the two glossary tables did, and
  Claude Code would read only `CLAUDE.md`.
- **A `CLAUDE.md` stub that imports `AGENTS.md`, or a symlink.** Rejected:
  it keeps a tool-specific file in the tree for a fallback the tool now
  provides, and a stub takes precedence over `AGENTS.md` for Claude Code.
  A symlinked `CLAUDE.md` is not workable because Claude Code does not
  write through it.
- **Move the glossary table into `AGENTS.md`.** Rejected: it would roughly
  double the instruction file with reference material most tasks do not
  need, and human readers would still look for terms in the architecture
  document.
- **Make `docs/architecture.md` the only glossary.** Rejected: the narrative
  ranks last under item 3 and is rewritten whenever the architecture moves;
  definitions belong in a document whose only job is definitions.
- **Leave the Section change to a later UX ADR.** Rejected: the decision is
  already taken and shipped, and without an ADR the corrected glossary entry
  loses to ADR-0058 and ADR-0084 from the day it is written.

## Consequences

### Positive

- Any two disagreeing documents have a stated winner.
- Terms are defined once; the architecture document and the instruction
  file can no longer contradict the glossary by carrying stale copies.
- The instruction file can be read by any coding agent and is short enough
  to be kept correct.
- The Section term matches the shipped navigation model and the regression
  guard that pins it.

### Negative

- The glossary is no longer loaded into every agent session automatically.
  An agent reads it when `AGENTS.md` sends it there. `AGENTS.md` therefore
  keeps the rule to use glossary terms precisely and to read
  `docs/glossary.md` before naming anything new.
- Claude Code's `AGENTS.md` support is a fallback with limits: a `CLAUDE.md`
  or `CLAUDE.local.md` in the working directory or any directory above it
  takes precedence; the fallback can be switched off in `/config`; and it
  was not available on every cloud deployment (Bedrock, Vertex, Foundry) at
  launch. An operator who sees a Claude Code session ignore `AGENTS.md`
  checks those three first.
- Thirty-six existing ADRs cite `CLAUDE.md` and are not edited. They are
  read as referring to `AGENTS.md` for rules and to `docs/glossary.md` for
  terms; the ADR index carries a note saying so.

### Neutral / Follow-ups

- No code, schema or migration change follows from this ADR beyond
  docstrings and comments that name the instruction file.
- Terms decided by ADR-0133 (briefing, family switch, sensitivity preset)
  enter the glossary with that ADR's build (item 2). The briefing entry must
  distinguish itself from the Decision Console briefing of ADR-0089.
- The roadmap and the rest of `docs/architecture.md` are reconciled in the
  same documentation cleanup, separately from this record.

## Implementation Notes

- Instruction file: `AGENTS.md` (repository root), replacing `CLAUDE.md`.
- Glossary: `docs/glossary.md`; `docs/translation-glossary.md` removed.
- `docs/architecture.md`: the "Canonical terminology" section becomes a
  pointer to `docs/glossary.md`; its sister-document list and precedence
  sentence follow item 3.
- References to the instruction file in `CONTRIBUTING.md`, in code and test
  docstrings, and in `docs/adr/README.md` are updated; accepted ADRs are
  not.
- Section catalogue: `web/shell.py` (`_AREAS`, `_SECTIONS_BY_AREA`,
  `sections_for`); view switching: `web/static/js/shell.js`; view frame:
  `web/templates/areas/_section.html`.
- Related test: `tests/regression/test_section_catalogue_matches_body_partials.py`.
- Related documentation: `docs/ux/ui-standards.md` §2.1 and §7.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (analysability
  and modifiability — one definition per term and one rule set, with a
  stated precedence).
- **Audit evidence:** this ADR and its index row; `AGENTS.md`;
  `docs/glossary.md`; the absence of `CLAUDE.md` from the tree; the section
  catalogue guard named above. Together they show that the change of
  instruction file, glossary location and Section definition was deliberate
  and traceable rather than silent drift (BAIT/VAIT change-management
  traceability).

## References

- ADR-0002 — Canonical Glossary (superseded by ADR-0084)
- ADR-0008 — English as the sole codebase language
- ADR-0015 — Claude-assisted development workflow (amended by this ADR)
- ADR-0058 — Web information architecture (superseded in part by this ADR)
- ADR-0084 — Glossary v2 (superseded in part by this ADR)
- ADR-0089 — Decision Console briefing UI (earlier use of "briefing")
- ADR-0133 — Watch Desk briefing artefact and watch-point scope
- `docs/ux/ui-standards.md` — UI standards, §2.1 navigation model
- Claude Code changelog, version 2.1.277 (2026-09-18) — `AGENTS.md` support

---

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-09-30 | PortfoliFLOW project owner | Initial draft; accepted. Amends ADR-0015 (instruction file `AGENTS.md`, no `CLAUDE.md`); supersedes ADR-0084 in part (glossary location, item 5 precedence, the Section sentence of item 1) and ADR-0058 in part (long-scroll sections and the section indicator). |
