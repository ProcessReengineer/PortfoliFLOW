# ADR Retrofit Verification Report — 2026-04-29

**Date:** 2026-04-29
**Scope:** `docs/adr/`, `CLAUDE.md`, `docs/architecture.md` — verification of the
2026-04-29 documentation pass that added ADR-0029, ADR-0030, ADR-0031
(Accepted) and ADR-0032 (Proposed); amended Revision-History rows on
ADR-0017 and ADR-0018; and applied surgical edits to `CLAUDE.md` and
`docs/architecture.md`.

## Overall verdict

**CLEAN** (all PASS).

## Summary

Verified the post-pass state of the documentation. Four new ADR files exist
with the agreed filenames and pass the *ADR style guide* bar set by
ADR-0011, ADR-0022, and ADR-0025. ADR-0029 / ADR-0030 / ADR-0031 are
`Accepted` and grounded in source code that is in the working tree;
ADR-0032 is `Proposed` and explicitly marks every affected path
`(planned)`. The two amended ADRs (0017, 0018) gained exactly one new
Revision-History row each, dated 2026-04-29, with no other content
changes. ADR-0025 is unchanged. The README index gained four new rows in
numeric order. The `CLAUDE.md` and `docs/architecture.md` edits were
applied verbatim with no surrounding content disturbed.

## Findings

### Check 1: New ADR files exist with correct filenames
**Status:** PASS

- `docs/adr/0029-headless-shirley-qt-free-entry-point.md` — exists.
- `docs/adr/0030-telegram-bot-as-first-headless-client.md` — exists.
- `docs/adr/0031-module-level-threading-lock-interim-concurrency.md` —
  exists.
- `docs/adr/0032-ui-theme-schema-extension-layout-pill-font.md` — exists.

All four follow the `NNNN-kebab-case-title.md` convention; all four have
heading text matching the agreed title.

### Check 2: Status fields and dates
**Status:** PASS

| ADR     | Status   | Date       | Required | Match |
|---------|----------|------------|----------|-------|
| 0029    | Accepted | 2026-04-29 | Accepted, 2026-04-29 | YES |
| 0030    | Accepted | 2026-04-29 | Accepted, 2026-04-29 | YES |
| 0031    | Accepted | 2026-04-29 | Accepted, 2026-04-29 | YES |
| 0032    | Proposed | 2026-04-29 | Proposed, 2026-04-29 | YES |

### Check 3: Template conformance for the four new ADRs
**Status:** PASS

For each of 0029, 0030, 0031, 0032 the following sections are present
and substantive (not placeholder):

- `## Context`
- `## Decision`
- `## Rationale`
- `## Alternatives Considered`
- `## Consequences` (with Positive, Negative, and Neutral / Follow-ups
  sub-sections in each ADR)
- `## Implementation Notes`
- `## Compliance & Audit Relevance`
- `## References`
- `## Revision History`

*Alternatives Considered* concrete-entry counts (lines beginning with
`- **` inside the section): ADR-0029 = 5, ADR-0030 = 7, ADR-0031 = 5,
ADR-0032 = 5. All meet or exceed the minimum of 3.

The *Negative Consequences* sub-section in each ADR is non-trivial and
names specific real costs (code duplication for ADR-0029; full tool
parity and Telegram privacy posture for ADR-0030; residual
bot-vs-GUI race for ADR-0031; lockstep update obligation across three
theme variants for ADR-0032). No ADR has an empty or pro-forma Negative
section.

### Check 4: ADR-0032 stays honest about its Proposed status
**Status:** PASS

- `## Status` is `Proposed`.
- `## Implementation Notes` contains nine `(planned)` markers, applied
  to every affected file path the section names. The opening sentence
  of the section reads: *"This ADR is `Proposed`. All affected paths
  below are **planned** and do not yet exist in the form described."*
- The section names existing files (the three theme JSONs,
  `core/ui_theme.py`, `gui/theme.py`) only as the *consumers /
  loaders* the planned schema extension will pass through, and
  explicitly states that none of them is modified by this ADR.
- *Compliance & Audit Relevance* under "Audit evidence" similarly
  marks every item planned and not yet present.

### Check 5: Cross-references — bidirectional resolution
**Status:** PASS

ADR-0029 references ADR-0010, ADR-0011, ADR-0012, ADR-0018, ADR-0022,
ADR-0030, ADR-0031.
ADR-0030 references ADR-0001, ADR-0018, ADR-0019, ADR-0022, ADR-0029,
ADR-0031.
ADR-0031 references ADR-0012, ADR-0017, ADR-0018, ADR-0019, ADR-0022,
ADR-0029, ADR-0030.
ADR-0032 references ADR-0021, ADR-0025 only.

Every referenced ADR file resolves against the working tree. The
required-references list in the prompt is satisfied for each ADR; some
ADRs include additional cross-references where the body discusses the
relationship substantively (e.g. ADR-0030 references ADR-0018 for the
FastAPI migration path; ADR-0031 references ADR-0017 for the related
DataStore concurrency follow-up). No fabricated or out-of-range
references.

ADR-0032 does **not** reference ADR-0001 (or any other ADR beyond
ADR-0021 / ADR-0025), and is itself **not** referenced from ADR-0025's
body — verified.

### Check 6: Existing-file path references resolve
**Status:** PASS

Every backtick-quoted file path inside the four new ADRs that is
presented as existing has been verified against the working tree:

- `services/headless_shirley.py` — exists.
- `services/ai_service.py` — exists.
- `services/tool_registry.py` — exists.
- `bot/__init__.py`, `bot/config.py`, `bot/telegram_bot.py` — exist.
- `main.py` — exists.
- `pyproject.toml` — exists.
- `.env.example` — exists.
- `tests/services/test_headless_shirley.py` — exists; the
  regression-guard test is `test_no_qt_import_in_fresh_subprocess`
  (verified in source) and the lock-serialisation test is
  `test_lock_serialises_concurrent_turns` (verified in source).
- `tests/bot/test_telegram_bot.py` — exists; the regression-guard
  test is `test_no_qt_import` (verified in source).
- `config/ui_theme.json`, `config/ui_theme_light.json`,
  `config/ui_theme_corporate_blue.json` — all three exist.
- `core/ui_theme.py`, `gui/theme.py` — exist.
- `docs/Soul_Shirley.md` — exists.

Forward-looking paths in ADR-0029 ("future `services/_tool_loop.py`")
and ADR-0030 ("future FastAPI handler") are clearly framed as
prospective and are not presented as existing. ADR-0032's affected
paths are all marked `(planned)` per Check 4.

### Check 7: ADR-0017 and ADR-0018 amendments
**Status:** PASS

Each file gained exactly one new Revision-History row dated 2026-04-29:

- ADR-0017's row records the cross-reference to ADR-0029 (Qt-free
  precondition for DataVault) and ADR-0031 (concurrency follow-up).
- ADR-0018's row records that ADR-0029 has delivered the first Qt-free
  service entry point, partially anticipating the service / repository
  split.

Both rows close with "Decision body unchanged." or equivalent. A
`git diff` confirms only the Revision-History table is touched; the
*Decision*, *Rationale*, *Alternatives Considered*, *Consequences*,
*Implementation Notes*, *Compliance & Audit Relevance*, and *References*
sections of both files are byte-identical to their pre-pass state.

### Check 8: ADR-0025 untouched
**Status:** PASS

`git diff HEAD -- docs/adr/0025-ui-theming-system-with-multi-variant-support.md`
returns no output. The file is byte-identical to its pre-pass state.
ADR-0032 references ADR-0025 in its *Decision* and *References* sections,
but no Revision-History row was added to ADR-0025 — Proposed ADRs do not
retroactively modify their parents.

### Check 9: README index updated
**Status:** PASS

`docs/adr/README.md` index now contains four new rows in correct
numeric order:

| #    | Title (verified against ADR heading) | Status   | Date       | Tags |
|------|---|---|---|---|
| 0029 | Headless Shirley as Qt-Free Synchronous Entry Point for Non-GUI Clients | Accepted | 2026-04-29 | architecture, integration |
| 0030 | Telegram Bot as First Non-GUI Client of Headless Shirley | Accepted | 2026-04-29 | integration, architecture, security |
| 0031 | Module-Level Threading Lock as Interim Concurrency Control for Bot-Side Turns | Accepted | 2026-04-29 | architecture, security, integration |
| 0032 | UI Theme Schema Extension for Layout, Pill, and Font Tokens | Proposed | 2026-04-29 | ui, architecture, process |

The previously-existing rows (0000–0028) are unchanged. No row points to
a missing file; no file is missing from the index.

### Check 10: Retrofit update report exists and tells the story honestly
**Status:** PASS

`docs/adr/0000-retrofit-update-2026-04-29.md` exists, is dated
2026-04-29, and is structurally analogous to
`0000-retrofit-update-2026-04.md`. It contains:

- A *Trigger* section naming the three threads of work (Etappe 1.A,
  Etappe 1.B, UI-polish thread).
- A *Source documents* section naming the two operator-supplied
  handover documents and the implementation files read for each new
  ADR.
- A *New ADRs* section that explicitly distinguishes the three
  Accepted ADRs from the one Proposed ADR with a one-paragraph summary
  of each.
- An *Existing ADRs touched* section confirming the Revision-History
  rows on ADR-0017 / ADR-0018 and the **explicit non-amendment** of
  ADR-0025.
- A *Cross-document changes* section listing the `CLAUDE.md` and
  `docs/architecture.md` edits with the rationale for each.
- An *Out of scope (deliberate)* section listing `readme.md`,
  `pyproject.toml`'s `bot*` packaging gap, the schema extension itself,
  and any other ADR.
- A *Debts acknowledged but not paid here* section restating the three
  handover-named debts (loop duplication; system-prompt-loader
  duplication; cross-channel concurrency gap) plus the
  planned-but-unimplemented status of ADR-0032.
- A *Re-evaluation of the carried-forward gap list* and a *Suggested
  follow-up ADRs* section.

### Check 11: CLAUDE.md edits applied verbatim
**Status:** PASS

- *Architecture in one paragraph* — the bot sentence ratified during
  bot implementation is present unchanged at the end of the paragraph.
- *Dependency rules* — the `bot/` bullet ratified during bot
  implementation is present unchanged.
- *Current project status* — two new lines appended after the
  `services/tools/chart_tools.py` line, before the
  `core/theme_service.py` line, in the established style:
  - `services/headless_shirley.py` — implemented (… see ADR-0029).
  - `bot/` — implemented (… see ADR-0030). Optional installation via
    the `bot` extra.
- *Implemented Services Reference* — two new sub-sections added after
  *Tool Classes and Trust Levels*: `### Headless Shirley (Qt-Free Turn
  Execution)` and `### Telegram Bot (Optional Non-GUI Channel)`. Both
  cross-reference the relevant new ADR(s).
- *What not to do — ever* — one new bullet appended forbidding `bot/`
  from importing `gui/` or `modules/`, with a `(see ADR-0030)`
  reference and a pointer to the regression-guard test.
- *Planned Architecture (Not Yet Implemented)* — one new sub-section
  appended after *Planned Feature Modules* and before *Architecture
  Review Protocol*: `### UI Theme Schema Extension (Layout / Pill /
  Extended Font)`, marked **planned, not yet implemented**, cross-
  referencing ADR-0032 explicitly. The deferral of `density` is named
  but no eventual ADR number is assigned. The other Planned
  Architecture sub-sections (DataVault, Separation of Concerns,
  Multi-User, Planned Feature Modules, Architecture Review Protocol)
  are unchanged.
- *Cross-references* — `(see ADR-0029)` / `(see ADR-0030)` /
  `(see ADR-0031)` are added at the appropriate one-per-topic-per-section
  granularity. ADR-0032 cross-references appear only inside the
  *Planned Architecture* sub-section, not elsewhere — confirmed by
  grep.

No structural rewrites of CLAUDE.md sections; the glossary, the
"three-line budget" rule, and the existing Planned Architecture
sub-sections are untouched.

### Check 12: docs/architecture.md edits applied verbatim
**Status:** PASS

- *Dependency rules* — the diagram block now contains the
  `bot/  →  core/ + services/` line; the bullet list contains the new
  `bot/` bullet mirroring the `analytics/` style.
- *Layer responsibilities* — a new ``### `bot/`` sub-section is placed
  after the ``### `services/`` sub-section. It cross-references
  ADR-0029 and ADR-0030, and contains the required short paragraph
  plus a one-row table.
- *Cross-cutting services* — a new bullet is appended for the Qt-free
  Shirley entry point, cross-referencing ADR-0029, ADR-0030, ADR-0031.

No content about the planned UI theme schema extension was added to
`docs/architecture.md` — that was deliberately scoped to `CLAUDE.md`'s
*Planned Architecture* section per the prompt.

### Check 13: Git scope discipline
**Status:** PASS (with one pre-existing modification noted for transparency)

`git diff --stat HEAD` shows the following changes attributable to this
documentation pass:

| File | Status | Note |
|------|--------|------|
| `CLAUDE.md` | M | All 7 edit groups applied. |
| `docs/adr/0017-planned-datavault-duckdb.md` | M | Single Revision-History row only. |
| `docs/adr/0018-planned-service-repository-layering.md` | M | Single Revision-History row only. |
| `docs/adr/README.md` | M | Four new index rows; existing rows unchanged. |
| `docs/architecture.md` | M | Three edit groups applied. |
| `docs/adr/0029-…md` | A | New file. |
| `docs/adr/0030-…md` | A | New file. |
| `docs/adr/0031-…md` | A | New file. |
| `docs/adr/0032-…md` | A | New file. |
| `docs/adr/0000-retrofit-update-2026-04-29.md` | A | New file. |
| `docs/adr/0000-retrofit-verification-report-2026-04-29.md` | A | This file. |

One pre-existing working-tree modification is also visible in
`git status`:

| File | Status | Note |
|------|--------|------|
| `repomix-output.xml` | M | Pre-existing modification, present in the working tree before this pass began (the initial `gitStatus` snapshot showed `M repomix-output.xml`). Not introduced by this pass. |

`readme.md` is unchanged — verified.

No code under `core/`, `services/`, `modules/`, `gui/`, `analytics/`,
`bot/`, or `tests/` is modified. Verified with
`git status --porcelain | grep -vE '^.[ M]?\s+(CLAUDE\.md|docs/|repomix-output\.xml)'`
which returns no output.

### Check 14: ADR-0025 not referenced from outside the Planned-Architecture sub-section
**Status:** PASS

`grep -n "ADR-0032" CLAUDE.md` returns matches only inside the new
*UI Theme Schema Extension (Layout / Pill / Extended Font)* sub-section
under *Planned Architecture*. No ADR-0032 cross-reference leaks into
any other section, in line with the prompt's guidance on not over-citing
a Proposed ADR.

### Check 15: Signs of truncation in new ADR files
**Status:** PASS

- ADR-0029: 248 lines, ends with a properly closed Revision-History
  table row.
- ADR-0030: 342 lines, ends with a properly closed Revision-History
  table row.
- ADR-0031: 250 lines, ends with a properly closed Revision-History
  table row.
- ADR-0032: 304 lines, ends with a properly closed Revision-History
  table row.
- 0000-retrofit-update-2026-04-29.md: 307 lines, ends with a properly
  closed Revision-History table row.

No file ends mid-sentence or mid-bullet. No empty files.

## Files requiring attention

None.

## Recommended next actions

No action required by this verification. Items below are recorded for
the operator's information; none of them is a verification failure.

1. The packaging gap noted in ADR-0030 (Negative Consequences) — `bot*`
   missing from `[tool.setuptools.packages.find]` in `pyproject.toml`
   — remains a separate code-change task, deliberately out of scope for
   this documentation pass.
2. The schema extension that ADR-0032 records is future work; an
   implementation prompt for it should be written when the operator is
   ready to begin the migration. ADR-0032 deliberately does not
   pre-write that implementation prompt.
3. `readme.md` revisions remain deferred to a separate session per the
   prompt's explicit out-of-scope statement.

---

**Pass / Fail line:**

**PASS.** All 15 checks pass. The documentation pass is structurally
complete, internally consistent with the ADR template and conventions
of ADR-0011 / ADR-0022 / ADR-0025, and honest about the
Accepted-vs-Proposed distinction.
