# ADR-0015: Claude-Assisted Development Workflow with Repomix and Model-Tier Split

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** process

---

## Context

PortfoliFLOW is built by a single developer working with AI assistants. To remain productive without losing architectural control, the workflow has to specify (a) which AI tools are used, (b) which model is used for which kind of task, (c) how the codebase is presented to the model, and (d) how human review fits in. Without an explicit workflow, model choice and prompting style drift, and reviewability of AI output deteriorates.

The project also has periodic "architecture review" moments — for example, before introducing a major new layer such as the planned DataVault (ADR-0017) — that benefit from a different prompting style than module-level implementation work.

## Decision

PortfoliFLOW uses Anthropic's Claude family as the primary AI assistant, with two distinct workflows:

1. **Module-level implementation** — AI generates code for a single Module (or Widget), starting from `docs/module_spec_template.md`, `core/base_module.py`, `modules/module_registry.py`, and the dependency rules. The session targets exactly one Module; the diff should touch at most three existing lines (ADR-0016).
2. **Architecture review** — A Repomix snapshot of the current codebase is submitted to Claude (the more capable tier, currently Opus) along with a focused review prompt (e.g., the one quoted in `CLAUDE.md` under "Architecture Review Protocol" for the upcoming DataVault work). The output is a list of findings; no code is generated in this mode.

Model-tier split: routine implementation work uses the default tier; architecture review and high-leverage decisions use the most capable tier.

`CLAUDE.md` is the project-level instruction file loaded automatically at the start of every Claude Code session and contains the conventions, glossary, and dependency rules.

## Rationale

- A model that has the entire repository in context (via Repomix) gives consistently better architectural feedback than one drip-fed individual files.
- Splitting model tiers by task type matches model capability to task difficulty without paying the highest cost on every change.
- An always-loaded instruction file (`CLAUDE.md`) is the most reliable mechanism to keep AI-generated code aligned with project conventions across sessions.
- The two-workflow split mirrors how the human developer actually works — implementation sprints vs. periodic architectural pauses.

## Alternatives Considered

- **Use one model tier for everything:** Rejected — either over-pays on routine work or under-performs on architecture review.
- **Generic AI tool with no project-specific instructions:** Rejected — every session would have to re-establish conventions, and the conventions would inevitably drift.
- **Free-form prompts without spec templates:** Rejected — module specs (`docs/module_spec_template.md`) are how the human stays in control of structure; without them, the AI invents its own.
- **Different AI vendor:** Implicitly not formally evaluated; the project's instruction file (`CLAUDE.md`) is named for the chosen tooling but the conventions themselves are vendor-neutral.

## Consequences

### Positive

- Implementation sessions stay narrowly scoped and easy to review.
- Architecture-review prompts produce specific, actionable findings rather than generic advice.
- New conventions can be added to `CLAUDE.md` and take effect on the next session without retraining.

### Negative

- The workflow assumes Repomix availability and a current Claude tier; both are external dependencies.
- `CLAUDE.md` itself becomes a critical artefact — bugs or contradictions in it propagate to every session.

### Neutral / Follow-ups

- Re-evaluate model tier as new Claude generations ship; the split (implementation vs. architecture review) is more durable than the specific tiers.
- The "Architecture Review Protocol" in `CLAUDE.md` is scheduled to run before DataVault implementation begins (see ADR-0017 / ADR-0018).
- If the project takes on additional contributors, document the workflow in a contributor guide, not only in `CLAUDE.md`.

## Implementation Notes

- Instruction file: `CLAUDE.md` (project root) — loaded automatically by Claude Code.
- Module spec template: `docs/module_spec_template.md`.
- Architecture review prompt: documented in `CLAUDE.md` under "Architecture Review Protocol".
- AI-collaboration safety net: the commit discipline in ADR-0014.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (changeability is governed by an explicit AI-collaboration process), Reliability (clean rollback path via ADR-0014).
- **Audit evidence:** `CLAUDE.md`, `docs/module_spec_template.md`, this ADR, and the Conventional Commits log (ADR-0014).

## References

- ADR-0014 (Conventional Commits and checkpoint discipline)
- ADR-0016 (Module-scope rule — the budget every implementation session is held to)
- ADR-0017 / ADR-0018 (Planned architectural changes that will trigger the next review pass)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing documentation; the workflow predates this ADR. |
