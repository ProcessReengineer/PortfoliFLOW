# ADR-0115: Rename the Decision Console Area to Watch Desk

- **Status:** Accepted (2026-08-11)
- **Date:** 2026-08-10
- **Tags:** watch-desk, decision-console, naming, ui, homepage, agpl-release-scope
- **Refines / clarifies:** ADR-0089 (the area this renames), ADR-0106 ("One
  Glass" refresh — its structure is untouched), ADR-0107 (Cases — the
  hand-off relationship is unchanged and gains a clearer division of labour).
- **Honours:** ADR-0008 (English-only codebase), the "Irene is internal only"
  rule (unchanged: the agent name never appears in user-facing surfaces),
  the DC4/D5 full-rename precedent (no title-only renames).

---

## Context

The area has carried the working title *Decision Console* since ADR-0089.
ADR-0107 already recorded the tension: "the word *Decision* in its name was
not yet earned: the Console observes and reports; it does not help decide."
The Cases area (ADR-0107) then took over the decision end explicitly — the
roadmap's own formulation of the division of labour is that the Console
"*watches and raises*, the Planning Desk *projects and simulates*", and Cases
work a raised finding to a documented close.

With ADR-0116 (watchpoint registry and signal families) the area's identity
sharpens further toward standing, autonomous observation. The name should say
what the section does. *Watch Desk* does: the maritime watch — someone calmly
holding watch, with responsibility, while the manager attends to other work —
matches both the calm-by-default design (ADR-0089) and the institutional
audience's expectations.

**Timing.** This is the last cheap moment for a rename. After the AGPL flip
(#052), the slug is a public URL, the module path is public API surface for
forks, and the docs are citable. A post-release rename would break all three.

## Decision

### 1. The area is named **Watch Desk**

Everywhere a user, reader, or contributor can see it:

- **UI:** area title, sidebar entry, section headings, empty states, flash
  and error strings, tooltips, `aria-label`s.
- **Slug and endpoints:** area slug `decision-console` → `watch-desk`;
  endpoint prefix `/api/decision-console/*` → `/api/watch-desk/*`. No
  redirects are kept: pre-release, nothing external references the old paths
  (the DC4 rename established this check — verify again at implementation
  that no deep link, template, or test fixture references the old slug
  outside the rename diff).
- **Code:** `modules/decision_console/` → `modules/watch_desk/`;
  `web/routes/decision_console.py` → `web/routes/watch_desk.py`; template
  partials `decision_console_*` → `watch_desk_*`; CSS class prefixes stay
  `pf-dc-*` (they are non-semantic tokens; churning every selector adds diff
  noise without renaming value — this is the one deliberate deviation from a
  byte-complete rename, recorded here so it is a decision, not an oversight).
- **Docs:** `readme.md`, `docs/architecture.md`, `docs/roadmap.md`,
  `CLAUDE.md` (area registry / `module_area` vocabulary), homepage copy on
  the live site (both domains deploy from this repository's homepage assets).
- **Monitor heading:** "What the console watches" → "What the Watch Desk
  watches" (or the shorter "On watch" — implementer's choice, recorded in
  the prompt).

### 2. What the rename does NOT touch

- **Accepted ADRs are immutable** (house rule). ADR-0085 through ADR-0114
  continue to say "Decision Console"; this ADR is the bridge a reader needs.
  The ADR index gains no retroactive edits beyond this ADR's own row.
- **"Irene" remains internal-only** and remains "Irene". The rename is about
  the public area name, not the agent.
- **Database identifiers** (`irene_*` tables, `module_area` values already
  persisted) are renamed only where cheap and safe at this stage:
  `module_area` registry value `decision_console` → `watch_desk` **is**
  renamed (it is pre-release data with a migration seam); the `irene_*`
  table names are **not** (they are agent-named, internal, and invisible).
- **The Cases hand-off, Journal semantics, cadence, and all ADR-0106
  structure** are untouched. This is a rename, not a redesign.

## Consequences

- One implementation prompt (P1 of the ADR-0116 programme) executes the
  rename before any watchpoint work, so ADR-0116's implementation is written
  against the new name and never needs a re-touch pass.
- `docs/architecture.md` module table, the areas registry, and the sidebar
  `_AREAS` ordering keep their positions; only the label and slug change.
- Grep acceptance at the end of the prompt: zero case-insensitive hits for
  `decision console` / `decision-console` / `decision_console` outside
  `docs/adr/` and this ADR's own references.

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-08-10 | PortfoliFLOW project owner + Claude | Drafted (Proposed). |
| 2026-08-11 | PortfoliFLOW project owner + Claude | Accepted; index status updated. |
