# ADR-0111 — Retire the four placeholder sections in Front Office and Back Office

**Status:** Accepted (2026-07-31)

## Context

Four sections in the area shell are pure placeholders with no implementation
behind them:

| Area | Section slug | Title | State in code |
|---|---|---|---|
| Front Office | `export` | Export | `section_pill="planned"`, static placeholder text only |
| Front Office | `timeseries` | Time Series | `section_pill="planned"`, static placeholder text only |
| Back Office | `cashflow` | Cashflow | `section_pill="planned"`, static placeholder text only |
| Back Office | `portfolio-tracking` | Portfolio Tracking | static `section_body` linking to `/investments` |

None of the four has a registered module (`modules/module_registry.py` carries
no `export`, `timeseries`, `cashflow`, or `portfolio_tracking` registration),
no route, no lazy-loading template, and no inbound deep link (`#export`,
`#timeseries`, `#cashflow`, `#portfolio-tracking` appear nowhere as anchor
references outside the partials and catalogue themselves).

The operator walkthrough for the UI Polish Pass (#053) established:

* **Export** and **Time Series** (Front Office): the functionality is no
  longer planned. Per-investment NAV, cashflow and total-return time series
  already render on the investment detail surface; report export lives with
  Investor Communication. A "planned" pill for work that will not happen
  misleads users and reviewers of the public AGPL release.
* **Cashflow** and **Portfolio Tracking** (Back Office): the intended
  functionality was realised elsewhere in the meantime (cashflow ledger and
  investment registry on the `/investments` surface). The placeholders are
  stale signposts.

Two consumers render from the section catalogue (`web/shell.py`
`_SECTIONS_BY_AREA`): the section-indicator navigation
(`section_index_for()`) and command search (`all_sections()`). The regression
guard `tests/regression/test_section_catalogue_matches_body_partials.py`
enforces that catalogue and body partials agree in slugs and order, so a
removal must land in the partial and the catalogue together.

Two secondary artifacts depend on the retired sections:

* The Back Office area subtitle enumerates "cashflow tracking, portfolio
  tracking, and strategic asset allocation" — two of three items would
  describe removed sections.
* `docs/module_specs/` carries specs for all four never-built modules
  (`export.md`, `timeseries.md`, `cashflow.md`, `portfolio_tracking.md`).

## Decision

1. **Remove the four sections** from their body partials
   (`web/templates/_partials/areas/_front_office_body.html`,
   `_back_office_body.html`) and from `_SECTIONS_BY_AREA` in `web/shell.py`,
   keeping partial and catalogue in lockstep so the catalogue guard stays
   green.
2. **Post-cut topology** (authoritative, superseding the section lists implied
   by ADR-0046 drafting language and the 6F-2 catalogue for these two areas):
   * Front Office: `overview`, `charts`, `statistics`, `portfolio-optimizer`.
   * Back Office: `saa`, `benchmarks-attribution`, `limits`.
   * All other areas: unchanged.
3. **Reword the Back Office subtitle** to describe the surviving sections:
   "Operations and reporting — strategic asset allocation, benchmarks &
   attribution, and investment limits."
4. **Archive the four module specs** to `docs/_archive/module_specs/`
   (house pattern for superseded documents; git history preserves
   provenance). They are not deleted: they document why the slugs existed.
5. **No functional relocation is part of this ADR.** The decision retires
   placeholders; it neither builds nor moves functionality. Should a report
   export or a dedicated time-series surface become a real feature later, it
   enters through the normal roadmap → ADR → mock pipeline under a fresh
   decision.

## Consequences

* The section-indicator dot count drops from 6 → 4 (Front Office) and
  5 → 3 (Back Office); command search stops offering the four phantom
  targets. Both consumers derive dynamically from `all_sections()`, so no
  code change is needed beyond the catalogue edit.
* `tests/regression/test_section_catalogue_matches_body_partials.py` and
  `tests/web/test_section_navigation.py` derive their expectations from
  `web.shell` at collection time and stay green without edits once partial
  and catalogue agree.
* The public release presents no "planned" pills for abandoned work in these
  two areas, and no signpost sections whose content lives elsewhere.
* Register: tracked as UI-S01 (Front Office pair) and UI-S02 (Back Office
  pair) in the structural section of the Chat-E findings register.
* Findings UI-010…022 from register v2 are unaffected: none of them targets
  a removed surface, so no re-disposition to `obsolete (surface removed)`
  results from this ADR.
