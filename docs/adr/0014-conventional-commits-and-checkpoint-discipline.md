# ADR-0014: Conventional Commits and Checkpoint-Commit Discipline Before AI Sessions

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** process

---

## Context

PortfoliFLOW is developed in short, AI-assisted sessions. Each session can produce a non-trivial diff that has to be reviewed and either accepted, partially accepted, or reverted. Two practices are necessary for this workflow to remain controllable:

1. A **predictable commit-message format** so that the project history can be skimmed, filtered (e.g., by `feat:` or `fix:`), and used as input for changelogs and AI summarisation.
2. A **clean working tree before any AI session** so that the diff produced by the AI is unambiguously *its* diff, separable from prior in-progress work and easy to revert with a single command.

Without these two habits, AI-generated changes mix with pre-existing edits, attribution becomes blurred, and rolling back a bad suggestion becomes risky.

## Decision

PortfoliFLOW commits follow **Conventional Commits** prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `style:`, `perf:`, `ci:`, `build:`. Each commit message states the change in the imperative ("add", "fix", "update").

Before starting an AI-assisted session, the working tree is in a clean state — either no uncommitted changes, or all in-progress work checkpointed in a commit. After an AI session, the diff is reviewed, refined if needed, and committed under the appropriate Conventional Commits prefix.

These conventions are enforced by review rather than by an automated commit-lint hook (today).

## Rationale

- A predictable prefix scheme makes `git log` useful for change-impact review, release notes, and AI-assisted summarisation. It costs nothing per commit and pays off on every retrospective.
- A clean working tree before AI sessions is the single most effective safety net for AI-generated changes — `git restore .` becomes a precise undo.
- Conventional Commits is widely adopted, has good ecosystem tooling (changelog generators, commit-lint), and is something AI assistants understand without prompting.

## Alternatives Considered

- **Free-form commit messages:** Rejected — inconsistency complicates skimming history and rules out off-the-shelf changelog tooling.
- **Commit ad-hoc, including AI sessions on top of in-progress work:** Rejected — blurs attribution and makes rollback fragile.
- **Adopt a different convention (gitmoji, custom):** Rejected — Conventional Commits is the industry standard and the one AI assistants emit by default.

## Consequences

### Positive

- Project history is greppable by change category.
- Rollback after a bad AI session is safe and quick.
- Future automated changelog generation is realistic without rewriting history.

### Negative

- Contributors must remember to checkpoint before invoking AI; an in-progress edit committed mid-AI-session is harder to attribute cleanly.
- Without a commit-lint hook, format violations are caught only at review time.

### Neutral / Follow-ups

- Consider adding a lightweight commit-lint hook (e.g., via pre-commit) once contributor count grows.
- Consider auto-generating a `CHANGELOG.md` from `feat:` / `fix:` / `perf:` commits at release time.

## Implementation Notes

- Convention enforced by review.
- Visible in the project's `git log` (e.g., recent commits — `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- Documented in: this ADR. (No prior in-repo documentation of the rule existed; the rule was observed.)

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (analysability — history is structured).
- **Audit evidence:** `git log` shows consistent prefixes; this ADR captures the convention.

## References

- ADR-0015 (Claude-assisted development workflow — relies on the checkpoint discipline established here)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from observed convention; the convention predates this ADR. |
