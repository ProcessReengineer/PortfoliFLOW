# ADR Retrofit Verification Report

**Date:** 2026-04-24
**Scope:** `docs/adr/` directory, post-retrofit state.

## Overall verdict

**CLEAN** (all PASS).

## Summary

Verified the post-retrofit state of `docs/adr/`. The directory contains 22 numbered Markdown files (`0000`–`0021`), one per ADR plus the meta retrofit report, alongside `README.md` and `template.md`. Every ADR file carries the full template structure, valid `Status` value, complete *Revision History* row, at least three concrete entries under *Alternatives Considered*, and only existing or explicitly-prospective file-path references. Inter-ADR cross-references all resolve. The README index has 22 rows and matches the directory exactly in number, title, and status. The retrofit report claims 21 ADRs (0001–0021) plus itself; the file inventory matches that claim with no truncation signs.

## Findings

### Check 1: File inventory
**Status:** PASS

- `docs/adr/` contains: `README.md`, `template.md`, `0000-retrofit-report.md`, and 21 numbered ADR files (`0001`–`0021`).
- Numbered file count: 22 (including `0000-retrofit-report.md`). Retrofit report claims "twenty-one Architecture Decision Records (ADR-0001 through ADR-0021)" plus itself — matches.
- All numbered files match the pattern `NNNN-kebab-case-title.md`. No stray, scratch, or temporary files present.
- This verification report (`0000-retrofit-verification-report.md`) is added by this run.

### Check 2: Numbering integrity
**Status:** PASS

- Numbers `0000`–`0021` are present, contiguous, no gaps, no duplicates.
- `0000` is reserved per the prompt and is the retrofit report (and now this verification report — the prompt mandates the verification report be written to a `0000-…` filename, so two `0000-…` files coexist by design; both serve meta purposes and neither is an ADR by lifecycle).

### Check 3: Template conformance
**Status:** PASS

For every ADR `0001`–`0021`, presence of all required sections was checked by literal-string grep:

- Header block: `**Status:**`, `**Date:**`, `**Deciders:**`, `**Tags:**` — all present in all 21 ADRs.
- Body sections: `## Context`, `## Decision`, `## Rationale`, `## Alternatives Considered`, `## Consequences`, `## Implementation Notes`, `## References`, `## Revision History` — all present in all 21 ADRs.
- Optional `## Compliance & Audit Relevance` — present in all 21 ADRs (every ADR includes the section).
- `Status` field values are within the allowed set (`Accepted` / `Proposed`); see Check 9 for per-ADR breakdown.
- *Alternatives Considered* concrete-entry counts (lines beginning with `- **` inside the section): minimum 3, maximum 5, no ADR below 3. Distribution: 0001 (4), 0002 (3), 0003 (4), 0004 (4), 0005 (3), 0006 (4), 0007 (4), 0008 (3), 0009 (4), 0010 (4), 0011 (4), 0012 (4), 0013 (4), 0014 (3), 0015 (4), 0016 (3), 0017 (5), 0018 (3), 0019 (4), 0020 (5), 0021 (4).

### Check 4: Referenced file paths exist
**Status:** PASS

Every backtick-quoted file path in every ADR was checked against the working tree. All concrete file references resolve. The following entries flagged by the regex are not failures and are documented for transparency:

- `docs/adr/0002` — `docs/glossary.md` is a forward-looking reference ("consider mirroring it into a dedicated `docs/glossary.md`"), not a present-tense claim.
- `docs/adr/0003` and `0016` — `__init__.py` references are generic Python module pattern (e.g., "the area's `__init__.py`"), not specific file paths. The actual `modules/<area>/__init__.py` files do exist.
- `docs/adr/0014` — `CHANGELOG.md` is a forward-looking suggestion ("Consider auto-generating a `CHANGELOG.md` …").
- `docs/adr/0017` — `core/datavault.py` is explicitly forward-looking ("a new `core/datavault.py` (or its own top-level `datavault/` package — to be decided)") in a Proposed ADR.
- `docs/adr/0021` — bare `chart_theme.json` mentions refer to the already-verified `config/chart_theme.json`.

Confirmed-existing files referenced in ADRs include (non-exhaustive): `CLAUDE.md`, `readme.md`, `pyproject.toml`, `docs/architecture.md`, `docs/module_spec_template.md`, `docs/Soul_Shirley.md`, `docs/chart_theme.md`, `core/base_module.py`, `core/data_store.py`, `core/exceptions.py`, `core/chart_theme.py`, `core/chart_helpers.py`, `modules/module_registry.py`, `modules/front_office/data_import.py`, `modules/back_office/saa.py`, `services/ai_service.py`, `services/ai_models.py`, `services/tool_registry.py`, `services/tools/datastore_tools.py`, `analytics/portfolio_optimizer.py`, `gui/main_window.py`, `gui/debug_data_window.py`, `gui/widgets/ai_settings_widget.py`, `gui/widgets/portfolio_analysis_widget.py`, `tests/front_office/test_data_import.py`, `config/chart_theme.json`.

### Check 5: Cross-references between ADRs
**Status:** PASS

Every `ADR-NNNN` reference in every ADR (and in `0000-retrofit-report.md`) was extracted and resolved against the file inventory. All references resolve to an existing ADR file with the matching number. No broken references; no references to numbers outside the 0000–0021 range. No `Superseded by` references present (all statuses are `Accepted` / `Proposed` — see Check 9).

### Check 6: Index in README
**Status:** PASS

- Index table in `docs/adr/README.md` contains 22 rows (one per file in the directory, including the retrofit report).
- Each row's number, title, and status match the corresponding ADR file's heading and `Status` field exactly.
- Statuses in the index: 17 Accepted (`0001`–`0016`, `0021`), 4 Proposed (`0017`–`0020`), 1 Informational (`0000`). Matches the per-file values.
- No ADR file is missing from the index. No index row points to a missing file.

### Check 7: Retrofit report completeness
**Status:** PASS

`docs/adr/0000-retrofit-report.md` contains all six required sections, each substantive (more than a placeholder sentence):

- `## Summary` (1 paragraph).
- `## Method` (sources read in full + drafting approach).
- `## Seed list deviations` (4 explicit "no" findings: no splits, no merges, no drops, no renames).
- `## Additional ADRs` (1 entry — ADR-0021 with justification).
- `## Gaps identified` (13 numbered entries).
- `## Suggested follow-up ADRs` (8 prioritised entries).

Plus a `## Cross-document follow-ups (not edited in this run)` section recording 4 non-ADR edits recommended for a separate change.

### Check 8: Scope discipline
**Status:** PASS

The retrofit report explicitly states "no code outside `docs/adr/` was modified" (Summary, line 12) and adds a "Cross-document follow-ups (not edited in this run)" section listing changes that were *not* applied. No claims of edits outside `docs/adr/`. Recommend the human run `git status` / `git diff` to confirm at the filesystem level — out of scope for this read-only check.

### Check 9: Status sanity
**Status:** PASS

Per-ADR status check against the seed list in the retrofit prompt:

- Seed list entries marked `Proposed` (Planned Architecture in `CLAUDE.md`): seeds 0017, 0018, 0019, 0020. Output: ADR-0017 = Proposed, ADR-0018 = Proposed, ADR-0019 = Proposed, ADR-0020 = Proposed. Match.
- Seed list entries marked `Accepted`: seeds 0001–0016. Output: all `Accepted`. Match.
- Additional ADR-0021 (Chart Theming): status `Accepted`. Sanity check — `config/chart_theme.json`, `core/chart_theme.py`, and `core/chart_helpers.py` all exist; the decision is reflected in the codebase. Status is consistent.

Spot-checks of `Accepted` ADRs against implementation:

- ADR-0003 — `core/base_module.py` (`BaseModule`, `VALID_AREAS`) and `modules/module_registry.py` (`ModuleRegistry`, `registry`) exist and embody the decision.
- ADR-0004 — `core/data_store.py` (`DataStore`, `get_data_store`) exists with the documented public API (`store`, `get`, `list`, `remove`).
- ADR-0010 — `services/ai_service.py` exists with the OpenAI-compatible client pattern; `services/ai_models.py` exists.
- ADR-0012 — `services/tool_registry.py` exists with `ToolRegistry` / `get_tool_registry`; `services/tools/datastore_tools.py` exists.

No contradictions found.

### Check 10: Signs of truncation
**Status:** PASS

- Every ADR file ends with a properly formatted *Revision History* table row (verified by inspecting the last line of each file).
- Line counts range 73–98 for ADRs and 97 for the retrofit report — all in the expected one-printed-page range.
- The retrofit report's claimed ADR count (21) matches the actual file count (21 ADRs + the retrofit report = 22 numbered files).
- No empty files (smallest is 4,200 bytes — `0008-english-as-the-sole-codebase-language.md`).
- No file ends mid-sentence or mid-bullet.

## Files requiring attention

None.

## Recommended next actions

No action required. The retrofit output is structurally complete and internally consistent.

Optional, low-priority follow-ups (not failures — recorded for the human's information):

1. Run `git status` / `git diff` to confirm at the filesystem level that the scope-discipline claim in the retrofit report (no edits outside `docs/adr/`) holds. The verification could not check this without a git diff.
2. The non-ADR cross-document edits enumerated in `0000-retrofit-report.md` ("Cross-document follow-ups") are deliberately deferred. When ready, action them in a separate commit per the original prompt's constraint.
3. The 13 gaps and 8 suggested follow-up ADRs in the retrofit report are decisions the project owner can act on at their own pace; none are flagged as blocking by this verification.
