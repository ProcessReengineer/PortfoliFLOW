# ADR-0029: Headless Shirley as Qt-Free Synchronous Entry Point for Non-GUI Clients

- **Status:** Superseded by ADR-0038
- **Date:** 2026-04-29
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, integration

---

> **Note (2026-05-03, Phase 1, Strang A2):** This ADR is superseded by
> ADR-0038. The role of the first Qt-free entry point is now fulfilled by
> `services/ai_service_core.py`, which is the only Qt-free core in the
> codebase. `services/headless_shirley.py` was removed in Strang A2 of the
> web migration; the module-level `_TURN_LOCK` from ADR-0031 was relocated
> into `services.ai_service_core` (still a `threading.Lock`, now serialising
> every consumer — Qt adapter, Telegram bot, and any future asyncio-native
> consumer — instead of bot turns alone). The Telegram bot now consumes
> `AIServiceCore.stream_response` directly. The original content below is
> preserved unchanged for historical reference.

## Context

Until 2026-04-28 the only way to drive Shirley's tool-execution loop was
through the AIService streaming worker (`services/ai_service.py::_StreamWorker`).
That worker is intrinsically Qt-coupled: it is a `QThread` subclass, it
emits `pyqtSignal` deltas to the GUI, and it expects to be parented and
finished from the Qt event loop. The exception is recorded in ADR-0011
and is appropriate for the GUI consumer.

The Telegram-bot work (see ADR-0030) introduced a second consumer that
cannot accept those constraints: it runs on its own asyncio event loop
inside a daemon thread, and any transitive PyQt6 import from that thread
breaks installations that have opted out of the GUI dependency surface
(or, more concretely, would force the bot to spin up a `QApplication` to
make `QThread` usable). The same constraint is anticipated for future
non-GUI callers — cron jobs, integration tests, and the FastAPI surface
sketched in ADR-0018.

The decision PortfoliFLOW must make is *how to expose Shirley's turn
semantics to non-Qt callers without rewriting the GUI path or coupling
non-Qt callers to Qt*. The Thursday demo timebox rules out a deep
refactor of `_StreamWorker.run` itself.

This decision touches Maintainability (introducing a deliberate, named
duplication) and Portability (this is the precondition for the planned
client-server topology in ADR-0018).

## Decision

PortfoliFLOW introduces a new module, `services/headless_shirley.py`, as
the Qt-free synchronous entry point to Shirley's tool-execution loop.
The module exposes a single public function:

```python
run_turn(
    prompt: str,
    *,
    client: openai.OpenAI,
    model: str,
    system_prompt: str,
    conversation: Conversation | None = None,
    temperature: float = 0.7,
    max_tool_iterations: int = 10,
) -> TurnResult
```

It also exposes two frozen dataclasses, `TurnResult` (the complete result
of one turn) and `ChartArtifact` (a single chart produced via the
`generate_chart` tool during the turn). These contracts are frozen
because callers — bots, future cron jobs, integration tests — must not
be able to mutate the turn result after the function returns.

The module honours four invariants:

1. **Qt-free.** It MUST NOT import from `PyQt6` in any form, transitively
   or otherwise. The regression-guard test
   `tests/services/test_headless_shirley.py::test_no_qt_import_in_fresh_subprocess`
   imports the module in a fresh subprocess and asserts that
   `"PyQt6"` is not present in `sys.modules` afterwards.
2. **Synchronous and blocking.** `run_turn` returns a complete
   `TurnResult` only when the model has produced its final text or the
   tool-iteration cap has been hit. There is no streaming surface; non-Qt
   callers want a complete result, not a stream of deltas.
3. **Same tool-execution semantics as the GUI.** The loop wraps every
   call in `ToolRegistry.begin_turn()` / `end_turn()` per ADR-0022,
   honours the iteration cap (default `_DEFAULT_MAX_TOOL_ITERATIONS = 10`,
   mirrored from `_MAX_TOOL_ITERATIONS` in `services/ai_service.py`), and
   recognises the same chart-artefact envelope shape as `_StreamWorker.run`.
4. **No implicit dependencies.** The OpenAI client, model ID, and system
   prompt are passed in as arguments. The function does not read
   QSettings, does not load `Soul_Shirley.md` from disk on its own, and
   does not consult the AIService singleton. Callers assemble these
   values themselves.

Concurrency is handled by a module-level `threading.Lock` named
`_TURN_LOCK`. Its scope, what it does protect, and what it explicitly
does not protect are recorded separately in ADR-0031.

PortfoliFLOW accepts that this module is a deliberate, time-bounded code
duplication of `services/ai_service._StreamWorker.run`. The two
implementations share control flow (dispatch through `ToolRegistry`,
detect chart artefacts, honour the iteration cap, bracket the turn in
`begin_turn` / `end_turn`) but differ in shape (streaming + Qt signals
vs. non-streaming + dataclass return). The unification path is recorded
in *Consequences* below.

## Rationale

- **The bot needs a Qt-free entry point.** Importing `_StreamWorker`
  from a non-Qt thread either drags `QThread` into a context that has
  no event loop or forces every non-GUI consumer to construct a
  `QCoreApplication`. Both options are worse than a documented
  duplication.
- **Synchronous beats streaming for non-GUI callers.** A bot, a cron
  job, or an integration test wants a complete `TurnResult` with chart
  artefacts already collected, not a stream of deltas it has to
  re-assemble. The shape of the result data class encodes that
  preference into the type system.
- **Auditability.** The Qt-free invariant is enforced by a regression
  guard that runs in a fresh subprocess. A future contributor who adds
  a transitive PyQt6 import — even an `if TYPE_CHECKING` branch that
  later becomes runtime — fails CI rather than corrupting the bot's
  import graph silently.
- **Demo-timebox aware.** Touching `_StreamWorker.run` is a
  GUI-critical-path change. With the Thursday demo on the schedule, a
  duplication that ships behind an enforced invariant is the
  lower-risk option. The duplication is named, located, and tracked
  here so the debt does not become invisible.
- **Architectural seam for ADR-0018.** The planned Service /
  Repository layering and eventual client-server topology require a
  Qt-free seam at exactly this level. `headless_shirley.run_turn` is
  that seam. Wrapping it later in a FastAPI handler or an asyncio
  service is additive, not a rewrite.

## Alternatives Considered

- **Stream Shirley turns through the GUI signal layer for the bot too
  (use `_StreamWorker` and adapt).** Rejected. The bot runs in a
  worker thread, not the Qt main thread, and threading Qt signals
  across a non-Qt event loop is more invasive than duplicating the
  loop. It would also require a `QCoreApplication` in every non-GUI
  consumer.
- **Asyncio-native rewrite of the AIService.** Rejected — out of scope
  for the demo timebox; would force every Qt consumer to integrate
  with an asyncio loop (e.g. via `qasync`), inverting the cost of the
  exception in ADR-0011.
- **Defer the bot until after the demo and unify the loop properly
  first.** Rejected — the demo benefits from showing the bot working
  alongside the GUI; the duplication is bounded and tracked here.
- **Refactor `_StreamWorker.run` first, then build the bot on the
  unified loop.** Rejected for the same demo-risk reason; recorded
  here as the unification target. The right shape is a callback-based
  pure function (see *Consequences — Neutral / Follow-ups*).
- **Keep the loop in `_StreamWorker` and have the bot call it via a
  thin Qt-aware façade.** Rejected — a façade does not satisfy the
  Qt-free invariant; the bot's import graph would still pull in
  `PyQt6.QtCore`.

## Consequences

### Positive

- Non-GUI callers have a typed, blocking entry point that returns a
  frozen `TurnResult`. The contract is small and easy to mock in tests.
- The Qt-free invariant is structural, not aspirational: a regression
  guard fails CI on any transitive PyQt6 import.
- `headless_shirley.run_turn` is the seam that ADR-0018's planned
  client-server layering can sit behind without re-architecting the
  GUI path.
- Future second channels (Signal bot, Slack bot, FastAPI handler) reuse
  the same function, not a per-channel re-implementation of the loop.

### Negative

- The tool-execution loop now exists in two places. Bug fixes and
  policy changes (e.g. a new envelope shape, a new gating rule) must be
  applied to both. The risk of drift is real, not theoretical.
- The system-prompt loader is duplicated between
  `AIService.get_system_prompt` and `bot.telegram_bot._load_system_prompt`
  for the same Qt-free reason. That duplication is documented in the
  bot's module docstring and travels with this ADR rather than with
  ADR-0030, since both duplications resolve in the same refactor.
- Headless callers must assemble the OpenAI client, model ID, and
  system prompt themselves. The bot does this in `bot/telegram_bot.py`;
  future callers will do it again. Centralising that assembly is a
  follow-up, not a precondition.

### Neutral / Follow-ups

- **Unification path.** Refactor the tool-execution loop into a pure
  function with a callback interface
  (`on_text_chunk`, `on_tool_call_started`, `on_chart_artifact`,
  `on_complete`). `_StreamWorker.run` becomes a thin adapter that
  bridges the callbacks to Qt signals; `headless_shirley.run_turn`
  becomes a thin adapter that bridges the callbacks to local lists.
  Target window: after the bot has stabilised in production use (two
  to four weeks of real operator usage) and before the planned
  client-server refactor of ADR-0018 begins. A premature refactor
  before the bot's interface stabilises would lock in the wrong
  abstraction.
- **System-prompt loader.** Bundle into the same refactoring wave as
  the loop unification — the cause is identical (the Qt-free
  invariant), so the fix should be too.
- **Concurrency.** See ADR-0031 for the lock that lives in this module.

## Implementation Notes

- Module: `services/headless_shirley.py` (full file, including module
  docstring sections "Intentional duplication", "Architectural
  constraints", and "Concurrency").
- Public surface: `run_turn`, `TurnResult`, `ChartArtifact` — all
  re-exported via `__all__`.
- Iteration cap: `_DEFAULT_MAX_TOOL_ITERATIONS = 10`, mirrored from
  `_MAX_TOOL_ITERATIONS` in `services/ai_service.py`. The constant is
  duplicated rather than imported because importing `services.ai_service`
  would pull PyQt6 into the module's import graph.
- Module lock: `_TURN_LOCK = threading.Lock()` — see ADR-0031.
- Regression guard: `tests/services/test_headless_shirley.py::test_no_qt_import_in_fresh_subprocess`.
- Contrasting GUI path: `services/ai_service.py::_StreamWorker.run`
  (no lock; same control-flow shape; Qt-coupled).
- Current consumer: `bot/telegram_bot.py::_handle_text_message` —
  see ADR-0030.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Maintainability
  (modularity is temporarily reduced — the duplication is named,
  located, and time-bounded; the named follow-up is the unification of
  the two loops), Portability (the Qt-free entry point is the
  precondition for the planned client-server migration in ADR-0018).
- **Audit evidence:** the module docstring in
  `services/headless_shirley.py`; the `test_no_qt_import_in_fresh_subprocess`
  regression guard; the cross-reference in `bot/telegram_bot.py`'s
  module docstring (which states the same Qt-free guarantee from the
  bot's side); this ADR.

## References

- ADR-0010 (AIService singleton — the Qt-coupled counterpart whose
  loop this module duplicates)
- ADR-0011 (Acknowledged PyQt6 dependency in AIService — establishes
  the layering exception that motivates a separate Qt-free seam)
- ADR-0012 (ToolRegistry as single seam — the registry that both the
  GUI and headless paths drive)
- ADR-0018 (Planned Service / Repository layering — the architectural
  seam this module is the precondition for)
- ADR-0022 (Tool Trust Classes and Gating Policy — the per-turn
  gating contract this module honours via `begin_turn` / `end_turn`)
- ADR-0030 (Telegram bot — the first consumer of `run_turn`)
- ADR-0031 (Module-level threading lock — the concurrency control for
  this module)

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-04-29 | PortfoliFLOW project owner   | Initial draft. Records the decision behind `services/headless_shirley.py`, the Qt-free invariant, the deliberate duplication of `_StreamWorker.run`, and the unification follow-up. Code already implemented and in use. |
| 2026-05-03 | PortfoliFLOW project owner   | Superseded by ADR-0038 (Phase 1, Strang A2). `services/headless_shirley.py` was removed; the Telegram bot was migrated to consume `services.ai_service_core.AIServiceCore.stream_response` directly; the ADR-0031 module-level `_TURN_LOCK` was relocated into `services.ai_service_core` and now serialises every consumer rather than bot turns alone. The "Unification path" follow-up named in *Consequences — Neutral / Follow-ups* is fulfilled by this retirement. Decider: PortfoliFLOW project owner. |
