# ADR-0002: Canonical Glossary — Area, Module, Feature, Function, Widget, Panel, Service

- **Status:** Superseded by ADR-0084
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** process, architecture

---

## Context

PortfoliFLOW is built collaboratively by a human developer and AI assistants. Generic words such as "function", "feature", "section", or "module" mean different things to different speakers and to different language models. When prompts and reviews mix those meanings, AI assistants produce code in the wrong layer, register modules in the wrong area, or change unrelated parts of the GUI.

The system also has terms ("Area", "Panel", "Widget") that map to specific code constructs. Without a single shared definition, those mappings drift over time, and the documentation diverges from the code.

## Decision

PortfoliFLOW uses a fixed seven-term canonical glossary, binding for all human and AI contributors:

- **Area** — one of the five top-level organisational groups: *Front Office, Back Office, Admin, Investor Communication, Assistants*. One Area = one Panel in the GUI = one directory under `modules/`.
- **Module** — a registered `BaseModule` subclass assigned to exactly one Area; appears as a sidebar entry.
- **Feature** — a planning-level capability, possibly spanning multiple Modules / Widgets / Functions. Not a code construct.
- **Function** — a Python `def` or method. Nothing else.
- **Widget** — a `QWidget` subclass under `gui/widgets/` rendering a Module's UI inside its Area's Panel.
- **Panel** — the right-hand container view (`gui/panels/`) shown for one Area; contains Widgets.
- **Service** — a class under `services/` that integrates an external system (LLM, document gen, data feed).

The glossary, with the canonical mapping table and a list of common substitutions to avoid, lives in `CLAUDE.md`.

## Rationale

- A single shared vocabulary is the cheapest mechanism to align AI-generated code with the actual architecture. It costs one short table; it saves correcting whole misplaced modules.
- The glossary intentionally separates planning terms (Feature) from code-construct terms (Module, Widget, Panel, Service, Function). This lets product-level discussions stay loose without polluting code-generation prompts.
- Pinning "Function" to mean *only* a Python function or method removes the most common source of ambiguity ("implement a new function" is otherwise ambiguous between a Feature and a Module).

## Alternatives Considered

- **Free-form terminology, clarify per prompt:** Implicitly rejected — terms drift between prompts, and the same word collected different meanings within a single review session.
- **Adopt a heavier vocabulary (e.g., DDD bounded contexts, aggregates):** Rejected as over-engineered for a single-developer project; the seven-term glossary covers the actual distinctions PortfoliFLOW needs today.
- **Define glossary outside the repository (e.g., wiki):** Rejected because AI assistants only reliably read what is checked in; the glossary belongs in the repository to be loaded into prompts.

## Consequences

### Positive

- AI prompts and code reviews share a precise vocabulary; ambiguous instructions can be challenged by reference to the table.
- New contributors and AI assistants can be onboarded by reading one section of `CLAUDE.md`.
- Architecture documentation stays aligned with the code because there is only one set of names.

### Negative

- Contributors must consciously avoid colloquial substitutions ("section", "page", "tab").
- The glossary must be updated when new structural concepts are introduced (e.g., if `gui/views/` were resurrected as a first-class concept).

### Neutral / Follow-ups

- The glossary is currently maintained in `CLAUDE.md`. If the project introduces a wider set of contributors, consider mirroring it into `docs/architecture.md` or a dedicated `docs/glossary.md`.

## Implementation Notes

- Source of truth: `CLAUDE.md`, section "Glossary — canonical terminology" (with the "Common mistakes to avoid" sub-table).
- Cross-referenced from: `docs/architecture.md` ("Canonical terminology").
- Code constructs the glossary maps to: `core.base_module.VALID_AREAS`, `gui/panels/`, `gui/widgets/`, `services/`, `modules/<area>/`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (analysability — shared terminology lowers cognitive load for code review).
- **Audit evidence:** The glossary is checked into the repository; deviations from it are visible at code-review time.

## References

- ADR-0001 (Layered architecture)
- ADR-0003 (BaseModule contract and ModuleRegistry)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing documentation; the original decision predates this ADR. |
| 2026-07-01 | PortfoliFLOW project owner            | Superseded by ADR-0084 — glossary evolved for the web variant (Section, Repository first-class; Widget/Panel demoted to legacy Qt). |
