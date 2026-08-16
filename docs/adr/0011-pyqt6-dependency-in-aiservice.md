# ADR-0011: Acknowledged PyQt6 Dependency in AIService for Signals and Threads

- **Status:** Superseded by ADR-0094 (was: Accepted; resolved by ADR-0038, 2026-05-03, which narrowed the exception from "the entire AIService file" to "the Qt adapter file (`services/ai_service_qt.py`) only")
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** architecture, integration, ui

---

## Context

The architectural rule established in ADR-0001 says `services/` imports from `core/` only. `AIService` (ADR-0010), however, performs two operations that the GUI must observe without blocking: streaming chat-completion deltas to the UI in real time, and fetching the model list from the endpoint. Both are naturally expressed using PyQt6's `QThread` for off-thread execution and `pyqtSignal` for thread-safe delivery to widgets.

The alternatives — building a framework-neutral threading and event abstraction inside `services/`, or deferring streaming to the GUI layer — have real costs. The first reinvents Qt poorly; the second forces every GUI consumer to re-implement worker/queue plumbing.

## Decision

`services/ai_service.py` is allowed to import from `PyQt6.QtCore` (`QThread`, `pyqtSignal`, `QObject`, `QSettings`). This is an explicit, documented exception to ADR-0001's "services depend only on core" rule. The exception is recorded both at the top of `services/ai_service.py` and in `CLAUDE.md`. No other file under `services/` is permitted to import PyQt6 without a corresponding ADR.

## Rationale

- Streaming UI updates and background model fetches are first-class requirements; building a framework-neutral abstraction in `services/` would be a strict downgrade from Qt's existing primitives.
- The exception is narrow: only `services/ai_service.py` is affected, and only the threading / signal primitives in `PyQt6.QtCore` are used.
- Keeping the exception explicit (rather than silently bending the rule) preserves the value of ADR-0001 elsewhere in `services/`.
- `QSettings` is also used here for persisting endpoint / model configuration; it integrates cleanly with the GUI's settings model and is part of the same Qt dependency.

## Alternatives Considered

- **Build a framework-neutral worker / signal layer in `core/`:** Rejected — the abstraction would either re-implement Qt or be insufficiently general; either way it adds maintenance burden for no clear benefit while there is one GUI framework.
- **Move the AIService into `gui/`:** Rejected — modules and (planned) headless callers must use the AIService too; placing it in `gui/` would invert the dependency rules in a more damaging way.
- **Move streaming into the GUI layer (`services/` would expose only blocking calls):** Rejected — every GUI consumer (Shirley, future Report Scraper review UI, etc.) would re-implement worker plumbing, defeating the centralisation goal of ADR-0010.
- **Use `asyncio` instead of QThread:** Rejected — would force PyQt6 widgets to integrate with an asyncio event loop (via `qasync` or similar), which is more invasive than allowing a narrow Qt import.

## Consequences

### Positive

- Streaming and background fetches are implemented with the right primitive for the platform.
- GUI consumers connect to `pyqtSignal`s without writing thread-management code.
- The exception is bounded and named; future audits know exactly where the layering is bent.

### Negative

- A future migration to a non-Qt frontend (web, CLI) requires re-implementing the AIService streaming surface or introducing an abstraction layer at that point.
- Headless / unit testing of `AIService` requires either a `QCoreApplication` or careful mocking of the worker classes.

### Neutral / Follow-ups

- If a non-Qt frontend becomes a real target (see ADR-0018 for the planned Service / Repository layering and client-server migration discussion), revisit this decision and either lift the exception with an abstraction or split AIService into a Qt-aware façade and a Qt-free core.
- No other `services/` module should add a Qt import without its own ADR amending this exception.

## Implementation Notes

- File: `services/ai_service.py` — see the explicit "Architecture note" comment at the top of the file.
- Worker classes: `_ModelsWorker`, `_StreamWorker` (both `QThread` subclasses).
- Signals exposed: `chunk`, `complete`, `error`, `tool_call_started`, `models_ready`.
- Persistence: `QSettings(_ORG, _APP)` for endpoint / model configuration.
- Documented in: `CLAUDE.md` ("AIService").

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability (modularity — exception is bounded), Portability (a future non-Qt frontend has more work to do because of this exception).
- **Audit evidence:** Source comment at the top of `services/ai_service.py`; this ADR.

## References

- ADR-0001 (Layered architecture and strict one-way dependencies)
- ADR-0010 (AIService singleton, OpenAI-compatible endpoints)
- ADR-0018 (Planned Service / Repository layering as prerequisite for client-server migration)
- ADR-0094 (GUI Sunset Execution — supersedes this ADR)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code and documentation; the original decision predates this ADR. |
| 2026-05-03 | PortfoliFLOW project owner            | Phase 1 / Strang A1 (ADR-0038): the named follow-up in *Neutral / Follow-ups* ("split AIService into a Qt-aware façade and a Qt-free core") is fulfilled. `services/ai_service_core.py` is now Qt-free and forms the canonical home for the OpenAI client, the tool-execution loop, and the per-turn `begin_turn` / `end_turn` brackets. The PyQt6 dependency is preserved only inside `services/ai_service_qt.py`, which remains an explicit exception to ADR-0001's "services depend only on core" rule. The Qt-free invariant of the core is enforced in CI by `tests/regression/test_ai_service_core_qt_free.py`. The bot-side counterpart (deletion of `services/headless_shirley.py`, ADR-0029) is deferred to Strang A2. |
| 2026-07-02 | PortfoliFLOW project owner            | **Superseded by ADR-0094** (GUI Sunset Execution, Stage 1). `services/ai_service_qt.py` — the sole remaining home of this ADR's PyQt6 exception after ADR-0038 narrowed it — was deleted together with the `gui/` surface and the `services/ai_service.py` shim, and PyQt6 left the dependency tree entirely. The exception this ADR carved out no longer applies to anything; the governing rule is now "no PyQt6 imports anywhere, no exceptions". The Qt-free `AIServiceCore` (ADR-0038) remains the live decision. |
