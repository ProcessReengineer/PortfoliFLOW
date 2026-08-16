# ADR-0110: Typing Island Set at CI Landing — Analytics Deferred

- **Status:** Accepted (2026-07-30)
- **Date:** 2026-07-30
- **Deciders:** Soenke (ProcessReengineer)
- **Supersedes in part:** ADR-0109 §3 (island set only; all other sections unchanged)
- **Roadmap:** #054

## Context

ADR-0109 §3 named three typing islands: `services/analytics/`,
`services/overlay/`, `services/market_data/`. The adoption run
(pyright 1.1.411, basic mode) measured: overlay 0 findings after
trivial fixes, market_data 0, **analytics 163** — all artefacts of
pyright inferring pandas types from source (pandas ships no
`py.typed`), none a real defect. Reaching zero would require either
~163 suppressions through the purity-guarded calculation core
(contradicting ADR-0109's own consequence that the typecheck contract
"claims exactly what the purity guards already enforce") or adopting
`pandas-stubs` (measured with `pandas-stubs==3.0.3.260530` against
pandas 3.0.2: analytics drops 163 → 10, but overlay re-opens 0 → 4,
for 14 findings tree-wide) — a distinct work item, not a trivial fix.

## Decision

1. At CI landing, `[tool.pyright] include` covers `services/overlay/`
   and `services/market_data/` only. `services/analytics/` enters the
   island set via `pandas-stubs` adoption, tracked as the first entry
   of #054's post-release typing note (target: analytics included with
   zero findings; the overlay regression from the stubs is fixed in
   the same work item).
2. An inline `# type: ignore` with a one-line reason is the sanctioned
   mechanism for imports that are unresolvable by construction
   (optional proprietary SDKs, e.g. `blpapi`); no config-level
   blanket suppression.

## Consequences

- The merge contract stays honest: pyright green means zero findings,
  not zero-after-suppression.
- ADR-0109 remains the CI contract; this successor records the one
  measured correction to §3 per the ADR immutability rule.
