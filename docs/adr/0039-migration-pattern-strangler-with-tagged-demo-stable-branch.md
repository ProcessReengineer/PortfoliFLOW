# ADR-0039: Migration Pattern — Strangler with Tagged Demo-Stable Branch

- **Status:** Accepted
- **Date:** 2026-05-03
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, process, architecture

---

## Context

ADR-0033 commits PortfoliFLOW to a migration from a PyQt6 desktop
application to a FastAPI-based web application and selects the
*strangler* pattern as the migration mechanism. This ADR records the
operational details of that pattern: the branch strategy, the demo-tag
discipline, the per-phase acceptance criteria, and which layers are
shared between the old and the new world.

A strangler migration runs the legacy system unchanged while the new
system is built around it. Functionality is moved across the seam in
small, individually demonstrable steps. The legacy system is removed
only after every responsibility it carried has been taken over and
validated. The opposite pattern — a "big-bang" rebuild followed by a
single switchover — was rejected in ADR-0033's *Alternatives Considered*
because it would interrupt demo capability for an extended period
during the soft-pitch phase.

The pattern is not new to PortfoliFLOW. ADR-0029 introduced
`services/headless_shirley.run_turn` as a Qt-free entry point that runs
the same tool-execution loop as `services/ai_service._StreamWorker.run`,
and ADR-0030 wired the Telegram bot as the first concrete consumer.
That pair already demonstrates, in production, that the project can
host two parallel surfaces around a shared backend with a Qt-free
invariant enforced by a regression guard. The web migration generalises
that pattern across the GUI surface; the same discipline applies, only
at larger scale and across more layers.

What this ADR fixes:

- **Branch strategy** — where the migration work happens and what
  shape `main` takes during the transition.
- **Demo discipline** — how a demo-stable state is recognised and
  recorded.
- **Per-phase acceptance** — the gate that a phase from ADR-0033
  passes before it is considered closed.
- **Shared vs. specific layers** — which directories are common to
  both worlds and which belong to exactly one.

What this ADR does not fix:

- **Phase content.** The list of phases and their substantive scope
  is in ADR-0033.
- **Web-tier technical detail.** Stack choices live in ADR-0034 to
  ADR-0038.
- **Final PyQt6 deprecation timing.** A separate ADR will record
  that decision when the parity criterion is met.

This decision is process- and architecture-relevant. Audit-wise, it
contributes the traceability evidence that BAIT/VAIT and ISO 25010
expect of a substantive architectural transition.

## Decision

1. **Strangler as the migration pattern.** The PyQt6 application
   continues to run unchanged for the duration of the migration. The
   web tier is built alongside it. Both consume the same backend
   layers. Functionality moves area by area; the legacy surface is
   not removed until every area has parity in the web tier and a
   stability period has elapsed.

2. **Shared layers.** `core/`, `services/`, `analytics/`, and
   `modules/` are common to both worlds. Changes to these layers
   must work for the PyQt6 GUI and for the web tier. Tests in these
   layers run once and apply to both consumers.

3. **World-specific layers.**
   - **PyQt6-specific:** `gui/` (widgets, panels, dialogs), the
     planned `services/ai_service_qt.py` adapter introduced by
     ADR-0038, Qt stylesheets generated from `config/ui_theme.json`.
   - **Web-specific:** `web/` (FastAPI application, routes, Jinja
     templates, static assets), the web CSS generated from the
     same `config/ui_theme.json`.
   - No code from the Qt-specific layers may import from the
     web-specific layers, and vice versa. The shared layers
     remain the only seam between the two worlds.

4. **Branch strategy.**
   - `main` is the demo-stable branch. Pull requests merge into
     `main` only when the resulting state is demo-stable.
   - `web-migration` is the long-lived working branch for the
     migration. Web-tier development happens here and on
     short-lived feature branches that target it.
   - Feature branches are cut from `web-migration` and merged
     back into it.
   - `web-migration` merges into `main` at the end of each phase
     (per ADR-0033), once that phase's demo-stable criterion is
     met.

5. **Demo-tag convention.** Demo-stable states on `main` are tagged
   with `demo-YYYY-MM-DD`. If multiple demo states are tagged on the
   same date, the suffix `-N` (one-based) disambiguates them
   (`demo-2026-06-15`, `demo-2026-06-15-2`, ...). Tags are
   immutable and document the points to which a pitch demonstration
   can be safely rolled back.

6. **Per-phase acceptance criteria.** A phase from ADR-0033 is
   considered closed when the following are true on `main` (or on a
   merge candidate that will become `main` immediately):
   - The PyQt6 application starts and is functional at no worse
     than the previous phase's level.
   - The web-tier endpoints promised by the phase are functional.
   - The full test suite is green for both the Qt path and the
     in-progress web tier.
   - The merge to `main` has been performed.
   - A `demo-YYYY-MM-DD` tag has been placed on the resulting
     `main` commit.

7. **Phase-end checklist** (the operational form of the criterion
   above; this is the literal list to walk through before tagging):
   - [ ] PyQt6 application starts cleanly.
   - [ ] PyQt6 application's previously working features remain
         functional.
   - [ ] Web tier serves the endpoints promised for the phase.
   - [ ] CI green on both paths (Qt and web).
   - [ ] Merge `web-migration` into `main`.
   - [ ] Tag `main` as `demo-YYYY-MM-DD` (or `demo-YYYY-MM-DD-N`).

8. **End-state criterion for PyQt6 deprecation.** Once every area
   from ADR-0033's phases has parity in the web tier and the web
   tier has been demo-stable or in production for at least three
   months, the PyQt6 layer becomes eligible for deprecation. The
   actual deprecation and eventual removal are decided in a
   separate ADR at that time. This ADR fixes the criterion, not
   the calendar.

## Rationale

- **Strangler over big-bang protects demo capability.** The
  soft-pitch phase requires the project to be demonstrable to
  prospective LPs and GP targets at any time. A big-bang migration
  would mean a multi-month "system is being rebuilt" gap during
  which there is nothing to show. Strangler keeps a fully
  demonstrable state at every commit on `main`.
- **Shared backend is the only economically viable shape.**
  Maintaining `core/`, `services/`, `analytics/`, and `modules/`
  twice — once for the desktop and once for the web — is not
  sustainable for a solo developer. Sharing them concentrates the
  parallel-maintenance cost in the GUI tier alone.
- **Tag convention beats branch convention for demo states.** A
  tag is an immutable point in history; a branch is a line of
  ongoing development. Demo states need to be reproducible exactly
  as they were shown, even months later. Tags are the right
  primitive.
- **Per-phase acceptance prevents drift into a half-migrated
  trap.** Without a hard gate at each phase, it is easy to merge
  partial progress that breaks the desktop variant in some
  edge case "for the duration of the next phase only." The gate
  forbids that. Either the phase is demo-stable on `main` or it
  is not closed.
- **Headless Shirley and the Telegram bot are the lived
  precedent.** ADR-0029 and ADR-0030 are the smaller-scale
  proof that this pattern works in PortfoliFLOW. The web
  migration is the larger application of the same idea, with
  the same discipline.
- **An end-state criterion (parity plus three months) is more
  honest than a date.** Calendar deadlines for solo-developer
  migrations are a poor predictor; a property-based criterion
  ("parity reached, then a stability soak") is what the project
  can actually commit to.

## Alternatives Considered

- **Big-bang migration.** Rejected in ADR-0033 for demo-capability
  reasons; recorded here as the principal alternative to the
  strangler pattern.
- **Feature flags inside `main` instead of a long-lived
  `web-migration` branch.** Considered. Feature flags are the
  right tool for incremental UI changes within a single
  architecture; they are not the right tool for a migration that
  introduces a new persistence layer (Postgres replacing
  in-memory), a new directory layout (`web/`), and a new transport
  surface (HTTP). Carrying the half-built web tier behind feature
  flags on `main` would either mean shipping disabled-but-loaded
  code (test- and security-overhead) or shipping it conditionally
  imported (which is the worst of both worlds: branchy and
  hidden). Rejected.
- **A separate repository for the web variant.** Considered. A
  clean `portfoliflow-web` repository would allow each side to
  evolve at its own pace. Rejected because the shared backend
  layers would then have to be packaged and versioned as a
  third-party dependency consumed by both repositories — a
  release-management mechanism the solo-developer setup cannot
  absorb. Within one repository, the shared layers are simply
  shared; across repositories, they become an artefact pipeline.
- **Mark demo states with branches, not tags.** Considered.
  Branches like `demo-pitch-2026-05` would carry the same
  information. Rejected because branches are not semantically
  immutable: a branch can be advanced or rebased, intentionally
  or by accident. A tag cannot, without an explicit force
  operation. The semantic shape of a demo state is "frozen point
  in history," which is what tags express.
- **Drop the per-phase demo gate and tag opportunistically.**
  Considered. Tagging only when convenient would reduce
  ceremony. Rejected because the discipline is precisely what
  prevents the half-migrated trap; relaxing it removes the gate's
  value.

## Consequences

### Positive

- Demo capability is preserved at every commit on `main` for
  the entire migration period.
- The branch and tag convention is explicit and reviewable.
  An auditor or a future contributor can locate every demo
  state by listing tags.
- The shared-backend rule keeps the parallel-maintenance cost
  bounded to the GUI tier.
- Per-phase acceptance creates natural review points for the
  developer (and any reviewer) to confirm that the migration is
  on track before committing to the next phase.
- The end-state criterion is property-based, not calendar-
  based — it is honest about the unknowns of a solo-developer
  migration.

### Negative

- Parallel GUI maintenance during the migration. Bugs in
  shared backend code that surface differently in the two GUIs
  must be reproduced and fixed for both surfaces.
- `web-migration` is a long-lived branch. Merge conflicts with
  `main` (driven by hotfixes that land on `main` directly) must
  be resolved regularly. The discipline of merging
  `web-migration` into `main` only at phase boundaries means
  that hotfixes on `main` need a separate, short-lived path
  back into `web-migration` — a small but real overhead.
- The phase gate is strict: an 80%-finished phase produces
  nothing mergeable. If a phase runs over schedule, the work
  sits on `web-migration` (and feature branches off it) until
  the gate is clearable.
- Tag proliferation. Multiple demo tags accumulate over the
  migration. Some discipline (or an `--all-tags` filter in CI
  cleanup) is required to keep the tag list legible.

### Neutral / Follow-ups

- **CI must run both worlds.** The build matrix for
  `web-migration` (and for any feature branch) needs to validate
  the Qt path and the in-progress web tier. The shape of that
  matrix is implementation work for Phase 1 and is not fixed
  here.
- **Hotfix routing.** A hotfix that lands on `main` outside a
  phase boundary should also be cherry-picked or merged into
  `web-migration` immediately, to avoid divergence. The exact
  workflow (cherry-pick vs. periodic `main → web-migration`
  merge) is a small process detail to settle in Phase 1.
- **Tag retention.** All demo tags are kept indefinitely
  during the migration. After PyQt6 deprecation, a clean-up
  policy can be considered; not in scope here.

## Implementation Notes

- **Pre-migration tag.** Before Phase 1 begins, `main` is
  tagged as `demo-pre-migration-2026-05-03` (or the date on
  which Phase 1 starts). This anchors the bottom of the
  demo-tag chain and gives an explicit reference point for
  "the state before the web migration."
- **Branch baseline.** `web-migration` is already cut from
  `main` at the time of this ADR. No code changes have landed
  yet on it that affect the web tier.
- **CI configuration.** The CI pipeline for `web-migration`
  must run the existing Qt-side test suite plus the
  in-progress web-tier tests. A failure on either path blocks
  merging into `main`. Detail belongs to the Phase 1 CI
  configuration work.
- **Phase-end checklist.** The list above is the literal form;
  it can be copied into a phase-end pull request description
  and ticked off there.
- **Tag command.** `git tag demo-YYYY-MM-DD` on the `main`
  commit at the phase boundary, followed by `git push origin
  demo-YYYY-MM-DD`.
- **Documentation.** This ADR is the authoritative description
  of the pattern; CLAUDE.md and `docs/architecture.md` will be
  updated in Phase 1 to point to it from their architecture
  sections.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (the shared-backend rule is the structural reason
  parallel maintenance stays bounded), Reliability (the
  demo-tag discipline yields reproducible reference states
  that survive every later change), Portability (the strangler
  pattern is the mechanism that delivers the cross-platform
  reach the migration is for).
- **Regulatory references:** BAIT/VAIT expect documented
  change-management discipline for substantive architectural
  transitions; the per-phase gate and the demo-tag chain are
  the form of that discipline in this project. DORA and
  MaRisk are not primarily addressed here — this ADR is a
  development-process artefact, not an operational-process
  artefact.
- **Audit evidence:** The list of `demo-*` tags in the Git
  history; the per-phase pull request descriptions that
  include the checklist; this ADR.

## References

- ADR-0029 (Headless Shirley as Qt-Free Synchronous Entry
  Point for Non-GUI Clients) — the strangler precedent at the
  AIService boundary.
- ADR-0030 (Telegram Bot as First Non-GUI Client of Headless
  Shirley) — the strangler precedent at the channel boundary.
- ADR-0033 (Web Migration: Architectural Shift from PyQt6
  Desktop to FastAPI Web) — the strategic frame whose phased
  plan this ADR operationalises.
- Martin Fowler, "StranglerFigApplication"
  (https://martinfowler.com/bliki/StranglerFigApplication.html) —
  external reference for the pattern itself.

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-05-03 | PortfoliFLOW project owner   | Initial draft. Records the strangler migration pattern, the branch and demo-tag convention, the per-phase acceptance criterion, and the end-state criterion for PyQt6 deprecation. No code changes; pattern only. |
| 2026-05-20 | PortfoliFLOW project owner   | Promoted to Accepted. The Strangler pattern was applied through Phases 1–6; the web-migration branch has been merged back to main. Closes P6-E. |
