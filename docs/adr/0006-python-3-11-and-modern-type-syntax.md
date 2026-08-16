# ADR-0006: Python 3.11+ with Modern Type Syntax and Mandatory Type Hints

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** process

---

## Context

PortfoliFLOW is a long-lived application with significant AI-assisted code generation. Type hints help both human reviewers and language models reason about call sites, return shapes, and possible None-cases without running the code. The project must pin a minimum Python version so that modern syntax (PEP 604 unions, PEP 585 generics, structural pattern matching) can be used uniformly without fallback imports from `typing`.

A formally chosen baseline also fixes the language-feature surface that AI-generated code is allowed to use.

## Decision

PortfoliFLOW targets Python 3.11 or newer (`requires-python = ">=3.11"` in `pyproject.toml`, `target-version = "py311"` in the `ruff` configuration). The codebase uses modern type syntax throughout:

- `str | None` instead of `Optional[str]`.
- `list[str]` and `dict[str, X]` instead of `List[str]` / `Dict[str, X]`.
- `match` statements where appropriate.

Type hints are mandatory on every function and method signature, including `__init__`. `Any` is used only where genuinely unavoidable, and only with a comment explaining why.

## Rationale

- Python 3.11 is supported on all PortfoliFLOW target platforms (Linux, macOS, Windows) and is widely available; pinning higher costs nothing today.
- Modern union and generic syntax is shorter and removes the need to remember which symbol comes from `typing`. It also matches the syntax most current AI assistants emit by default.
- Mandatory type hints make AI-generated code self-documenting and let `ruff` plus IDE tooling catch obvious mistakes without a separate type-checking pass being required at every step.
- A single language-version target avoids "works on my machine" failures from features available in newer interpreters but not the project baseline.

## Alternatives Considered

- **Target Python 3.10:** Rejected — gains nothing in compatibility (3.11 is widely available) while ruling out 3.11-only ergonomics (`Self`, exception groups, faster CPython).
- **Target Python 3.12 / 3.13:** Implicitly rejected — would shrink the runtime audience without a concrete need for the newer features today.
- **Allow legacy `typing.Optional` / `typing.List` for consistency with older codebases:** Rejected — mixing styles is worse than either style consistently.
- **Make type hints optional / advisory:** Rejected — type hints are part of the contract that makes AI-generated diffs reviewable.

## Consequences

### Positive

- Code is readable and self-documenting.
- IDE inference and AI-assisted code generation produce more reliable results.
- Single, consistent style across the codebase.

### Negative

- Contributors must consciously write type hints; reviewers must enforce them.
- The 3.11 floor blocks contributors stuck on older interpreters.

### Neutral / Follow-ups

- A static type checker (mypy / pyright) is not yet wired into CI. Adding one is a candidate follow-up; the codebase is already typed enough to make adoption realistic.
- Re-evaluate the version floor on each Python release; bump only when a concrete feature warrants it.

## Implementation Notes

- `pyproject.toml` — `requires-python = ">=3.11"`, `[tool.ruff] target-version = "py311"`.
- Documented in: `CLAUDE.md` ("Code conventions").
- Visible in: type annotations throughout `core/`, `services/`, `analytics/`, `modules/`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (analysability), Portability (the runtime floor is explicit).
- **Audit evidence:** `pyproject.toml` requires-python field; `ruff` configuration; code inspection.

## References

- ADR-0007 (Google-style docstrings on all public APIs)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code and documentation; the original decision predates this ADR. |
