# ADR-0038: AIService Refactoring — Qt-Free Core with Qt Adapter

- **Status:** Accepted
- **Date:** 2026-05-03
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, ai-service, refactoring, qt-decoupling

---

## Context

ADR-0011 acknowledged a deliberate violation of the
`services/` → `core/` only rule: `services/ai_service.py` imports
`PyQt6.QtCore` (`QObject`, `QThread`, `pyqtSignal`, `QSettings`)
because streaming chat-completion deltas to the GUI is most cleanly
expressed with Qt's threading and signal primitives. The exception
was bounded explicitly: it applies to that one file, and the ADR
named the resolution path —

> "If a non-Qt frontend becomes a real target [...] revisit this
> decision and either lift the exception with an abstraction or
> split AIService into a Qt-aware façade and a Qt-free core."

ADR-0029 (Headless Shirley) and ADR-0030 (Telegram Bot) introduced
the second consumer of Shirley's tool-execution loop. Rather than
splitting `AIService` then, the project shipped
`services/headless_shirley.py` as a deliberate, ADR-tracked
duplication of the loop in `_StreamWorker.run`. The decision was
demo-timebox-driven and explicitly named the unification target:
collapse the two implementations into a callback-based pure
function that both Qt and non-Qt callers could adapt.

ADR-0033 commits to the web migration. The FastAPI surface is the
**third** consumer of Shirley's turn semantics. Continuing to
duplicate the loop for each new consumer is no longer defensible:
two duplications were a tracked debt; three would be a pattern.
ADR-0011's named follow-up has come due.

The state today, summarised:

- `services/ai_service.py` (834 lines) houses three Qt-coupled
  pieces: `_ModelsWorker(QThread)`, `_StreamWorker(QThread)`, and
  `AIService(QObject)`. The latter exposes `pyqtSignal`s
  (`response_chunk`, `response_complete`, `tool_call_started`,
  `chart_generated`, `connection_status_changed`, `models_loaded`,
  `error_occurred`).
- `services/headless_shirley.py` (477 lines) duplicates
  `_StreamWorker.run`'s control flow as a synchronous, Qt-free
  function for the Telegram bot.
- A module-level `threading.Lock` in `headless_shirley.py` (per
  ADR-0031) serialises bot turns against each other but **does not**
  serialise against GUI turns through `_StreamWorker`. The
  cross-channel race named in ADR-0031 is one of the things the
  unification can finally close.

The web migration adds an asyncio-native consumer (FastAPI) and an
SSE wire format (per ADR-0037). A clean refactoring delivers four
things: it discharges ADR-0011's follow-up, it removes the
duplication ADR-0029 named, it gives ADR-0037's SSE endpoint a
natural source of events, and it sets up a single point at which
ADR-0031's cross-channel concurrency can be addressed when the
desktop variant is deprecated.

This ADR specifies the refactoring at the architectural level. It
does not perform the refactoring — implementation is Phase-1 work
under ADR-0033's plan and must satisfy the Phase-1 demo-stable
criterion of ADR-0039.

This decision is engineering-shaped rather than compliance-shaped;
the relevant ISO 25010 attributes are Maintainability, Modularity,
and Testability. Tool-trust gating remains governed by ADR-0022
and is preserved unchanged in the refactored core.

## Decision

### 1. File split

`services/ai_service.py` is split into two files:

- **`services/ai_service_core.py`** — Qt-free, asyncio-based.
  Owns: OpenAI / OpenRouter client construction; the
  tool-execution loop; soul-identity injection
  (`docs/Soul_Shirley.md`); `ToolRegistry` integration including
  the per-turn `begin_turn` / `end_turn` brackets that ADR-0022
  prescribes; chart-artefact recognition; the streaming token
  pipeline; and the configuration surface that today lives
  behind `connect()` / `set_model()` / `get_model()` /
  `get_status()`.
- **`services/ai_service_qt.py`** — thin Qt adapter. Owns: a
  `QObject` subclass that holds (or constructs on demand) an
  `AIServiceCore` instance, drives `stream_response()` to
  completion, and re-emits each event as the corresponding
  `pyqtSignal` (`response_chunk`, `response_complete`,
  `tool_call_started`, `chart_generated`, `error_occurred`). The
  GUI's import surface is preserved; existing `QObject.connect`
  call sites need at most an import-path change.

The bridge between the asyncio loop in the core and the Qt event
loop in the adapter is an implementation detail (`qasync` is the
leading candidate; a dedicated background thread running its own
asyncio loop with a thread-safe queue feeding `QMetaObject.invokeMethod`
is the conservative alternative). Both paths are admissible; the
implementation chooses one and documents the trade.

### 2. `StreamEvent` as the canonical wire shape inside the process

The core's streaming surface is an async generator yielding
`StreamEvent` records. The conceptual shape (final naming and
typing land in implementation; either dataclass or Pydantic model
is acceptable):

```python
@dataclass(frozen=True)
class StreamEvent:
    type: Literal["token", "tool_call_started", "tool_call_completed",
                  "chart_artifact", "complete", "error"]
    content: str | None                    # token text, error message, ...
    metadata: Mapping[str, Any]            # tool name, arguments, chart payload, ...
```

Every other consumer-facing shape is a thin adapter over this
generator:

- **PyQt6 GUI:** `ai_service_qt.py` translates each event to the
  corresponding `pyqtSignal`.
- **FastAPI / web (per ADR-0037):** an SSE endpoint serialises each
  event to a `text/event-stream` frame.
- **Telegram bot (per ADR-0030):** an asyncio consumer collects
  tokens, edits the message in place, and forwards chart artefacts
  as photos.
- **Synchronous batch callers** (one-shot extraction, future cron
  jobs): a small helper drains the generator and returns the
  collected text plus artefacts as a frozen result, mirroring
  today's `headless_shirley.TurnResult`.

### 3. Tool-execution loop: structurally unchanged

The control flow today is well-tested: dispatch tool calls through
`ToolRegistry`, detect chart-artefact envelopes, honour the
iteration cap (`_MAX_TOOL_ITERATIONS = 10`), bracket the turn in
`begin_turn` / `end_turn`. The refactoring lifts that loop into
`ai_service_core.py` **without changing its semantics**. Combining
a structural split with semantic changes to the loop would be risk
multiplication; the unification follow-up named in ADR-0029 is the
goal here, not an opportunistic rewrite.

ADR-0022's trust-class gating remains in the core. The follow-up
that ADR-0036 named — a per-user / per-role overlay on top of the
trust classes — is **not** anticipated by this refactoring; that
ADR is a separate decision.

### 4. Singleton lifecycle

`AIServiceCore` keeps the singleton shape `AIService` has today.
One instance per application process; `get_ai_service_core()` is
the accessor. The Qt adapter is also a singleton, but it is
**optional** — instantiated only when the PyQt6 GUI is running.
Web and bot consumers obtain the core directly and never see the
adapter.

### 5. Migration of existing consumers

- **PyQt6 GUI.** Imports change from `services.ai_service.AIService`
  to `services.ai_service_qt.AIServiceQt` (or the chosen final
  name). Existing `QObject.connect(...)` call sites continue to
  work because the adapter exposes the same signal names with the
  same payload shapes.
- **`services/headless_shirley.py`.** Re-evaluated during the
  refactoring. Its raison d'être (Qt-free entry point) is
  satisfied directly by `AIServiceCore.stream_response()`; its
  `TurnResult` shape and module-level lock may still be useful as
  a convenience layer for synchronous callers and as the home for
  ADR-0031's lock. The implementation chooses one of three:
  - keep it as a thin convenience wrapper around the core's async
    generator (simplest migration);
  - keep its public API (`run_turn`, `TurnResult`) but reimplement
    the body to delegate to the core (preserves bot import sites
    unchanged);
  - retire it and have the bot call the core directly (most
    architectural cleanup).
  This ADR commits to one of the three at implementation time;
  the bot's call site is one statement either way.
- **Telegram bot (ADR-0030).** Either continues to call
  `headless_shirley.run_turn` (if the helper is retained) or
  switches to a small asyncio-side consumer that drains
  `AIServiceCore.stream_response()` directly. The whitelist /
  config / lifecycle behaviour from ADR-0030 is not touched.
- **FastAPI web backend (Phase 2).** New consumer; constructed
  directly against `AIServiceCore`. The SSE endpoint described in
  ADR-0037 is the wire shape.

### 6. Asyncio as the core's concurrency model

The core runs on an asyncio event loop. FastAPI is asyncio-native;
modern Telegram-bot frameworks (aiogram, python-telegram-bot v20+)
are asyncio-native; both consume the core without a thread bridge.
The PyQt6 GUI consumes through the adapter, which spans the asyncio
↔ Qt boundary.

### 7. Test strategy (minimum requirements; final shape in implementation)

- **Core unit tests.** Pure asyncio, no Qt. Mock the OpenAI client
  at the boundary. Cover: streaming token aggregation, tool-call
  dispatch, iteration cap, chart-artefact recognition,
  `begin_turn` / `end_turn` bracketing, error pathways. The
  Qt-free invariant of `AIServiceCore` is enforced by a fresh-
  subprocess regression guard analogous to the one ADR-0029
  introduced for `headless_shirley`.
- **Qt adapter tests.** Run under `pytest-qt`. Cover: every
  `StreamEvent` variant produces the right `pyqtSignal` emission;
  signal payloads match the legacy shapes the GUI depends on.
- **Integration smoke tests.** One PyQt6 path through the adapter,
  one web path through SSE, both exercising the same recorded
  fixture against the mocked OpenAI client.

### 8. Backward compatibility during the refactoring

Phase 1 (per ADR-0033) is the window for this work. Throughout
Phase 1 the GUI, the Telegram bot, and `headless_shirley.py` must
remain functional. The refactoring is in-place and incremental;
no big-bang switch-over. The Phase-1 demo-stable tag required by
ADR-0039 is the binding gate.

### 9. ADR-0011 follow-up explicitly fulfilled

This ADR fulfils the follow-up announced in ADR-0011 ("split
AIService into a Qt-aware façade and a Qt-free core"). On
acceptance and implementation, ADR-0011's "Neutral / Follow-ups"
note is satisfied; the layering exception in `services/` shrinks
from "the entire AI service" to "the Qt adapter file only", which
remains an acknowledged exception at a much smaller surface.

## Rationale

- **Async generator over a custom streaming abstraction.**
  `AsyncGenerator` is the Pythonic primitive for streaming. It is
  framework-neutral and trivially adaptable to Qt signals, SSE
  frames, and Telegram message edits. A bespoke abstraction would
  be more invention than the problem requires.
- **Lift the loop unchanged.** The structural split is itself a
  non-trivial refactor. Combining it with control-flow changes
  would mean two distinct sources of risk in one Phase-1
  deliverable. Once the split is stable, opportunistic loop
  improvements become a localised follow-up.
- **Preserve the singleton shape.** Two existing patterns (GUI
  Qt-singleton, bot module-level call site) and one new pattern
  (FastAPI request handlers) are all comfortable with a per-process
  singleton core. Removing the singleton would propagate changes
  into every consumer for no concrete payoff.
- **Adapter is optional, not mandatory.** Headless deployments
  (FastAPI alone, bot alone, future cron job) should not pay for
  Qt at import time. Loading the adapter only in GUI processes
  preserves that property and reuses the Qt-free invariant
  technique that ADR-0029 already enforces in CI.
- **Re-evaluate `headless_shirley.py` rather than presuming its
  fate.** The module's value beyond "Qt-free entry point" is its
  `TurnResult` shape and the concurrency lock from ADR-0031. A
  blanket retirement would discard those without weighing them;
  a blanket retention would carry the duplication the ADR is
  trying to eliminate. The implementation chooses, with the
  decision recorded in code commentary or, if the choice is
  architecturally significant, in a follow-up ADR.
- **Asyncio in the core; Qt only at the adapter.** The strangler
  pattern's endgame is a Qt-less PortfoliFLOW; converging the
  core on asyncio now reduces the friction at that endgame. The
  existing PyQt6 path costs an extra hop through the adapter,
  which is a small price for a single-source-of-truth core.

## Alternatives Considered

- **Keep `headless_shirley.py` indefinitely as the bridge.**
  Rejected. The duplication ADR-0029 named would persist and the
  third consumer (FastAPI) would be its third instance. ADR-0011's
  follow-up was named precisely to prevent this.
- **Eliminate the Qt adapter entirely; rewrite the GUI to consume
  the async generator directly.** Rejected for Phase 1. PyQt6
  widgets consume `pyqtSignal`s; a direct async-generator
  consumption would force substantive GUI surgery during a
  refactoring whose primary goal is decoupling, not GUI rewrite.
  Open as an option once the desktop variant is on the
  deprecation path under a future ADR.
- **Threading-based core instead of asyncio.** Rejected. Asyncio
  is the natural match for the two new consumers (FastAPI, modern
  Telegram libraries). Threading would require its own bridge in
  both, eroding the unification gain. Python's GIL provides no
  CPU concurrency advantage that would offset the asymmetry.
- **Out-of-process gRPC / HTTP between core and consumers.**
  Rejected. Massive over-engineering for an in-process service.
  Solo-developer overhead unjustified by any current need.
- **Two fully separate service implementations (Qt and web).**
  Rejected. Reverts to the duplication problem; ADR-0011 named
  this as the wrong direction in advance.
- **Refactor `_StreamWorker.run` in place without splitting the
  file.** Considered but discarded. Untangling the Qt imports
  inside a single-file service is harder than the split itself,
  and the import surface (`from PyQt6.QtCore import ...`) is
  exactly what the headless consumers cannot tolerate. The split
  is what makes the Qt-free invariant testable.

## Consequences

### Positive

- ADR-0011's follow-up is fulfilled. The layering exception
  shrinks to the adapter file alone.
- The duplication named in ADR-0029 is closed. Bug fixes and
  policy changes that touched both the GUI loop and
  `headless_shirley.run_turn` now live in one place.
- ADR-0022's gating policy gains a single, canonical home.
- The web frontend gets a natural source of events for the SSE
  endpoint specified in ADR-0037; the bot keeps working with at
  most a one-line call-site change.
- Core unit tests run without `QApplication`, simplifying CI.
- The GUI's `pyqtSignal` consumer surface is unchanged; the
  Phase-1 demo-stable property (ADR-0039) is achievable without
  touching widget code beyond imports.

### Negative

- The refactoring is substantial. An 800-line Qt-coupled file
  becomes a Qt-free core plus a Qt adapter, both of which need
  careful test coverage before the GUI path can be repointed.
- The asyncio↔Qt bridge is a new failure surface. Whether it is
  `qasync` or a hand-rolled background-thread loop, lifecycle
  details (loop start, clean shutdown on app quit, thread-safe
  signal emission) must be exercised explicitly.
- `headless_shirley.py`'s future is unresolved by this ADR. The
  three options in §5 each leave residue: a thin wrapper still
  duplicates a small amount of logic; an aggressive retirement
  forces ADR-0031's lock to relocate; an in-place reimplementation
  preserves call sites but obscures the cleanup. The
  implementation must choose deliberately.
- ADR-0011's exception narrows but does not vanish: the Qt
  adapter file legitimately imports PyQt6, and that file becomes
  the new bounded scope of the layering exception.

### Neutral / Follow-ups

- Soul-identity injection (`docs/Soul_Shirley.md`) and
  `ToolRegistry` wiring move into the core unchanged in
  semantics.
- Additional model-provider backends (local Ollama, alternative
  OpenAI-compatible providers) become a core-internal extension
  point; not opened by this ADR but newly accessible.
- ADR-0031's cross-channel concurrency limitation has a clearer
  resolution path once the loop lives in one place; whether that
  resolution lands in this refactoring or in a follow-up depends
  on the implementation's appetite for scope.
- A future ADR may retire the Qt adapter once the desktop variant
  is deprecated; that decision belongs to its own ADR at that
  time.

## Implementation Notes

- **Suggested order (not binding).**
  1. Create `services/ai_service_core.py` empty; add a no-op
     module-level `__all__` and a fresh-subprocess Qt-free
     regression guard test.
  2. Move the OpenAI / OpenRouter client construction and
     `connect()` / `set_model()` / `get_model()` /
     `get_status()` surface into the core.
  3. Lift the tool-execution loop body (currently inside
     `_StreamWorker.run`) into a core async generator,
     preserving control flow.
  4. Add the `StreamEvent` shape and re-shape the generator's
     yields accordingly.
  5. Build `services/ai_service_qt.py` as the adapter, consuming
     the generator and emitting `pyqtSignal`s with the legacy
     payload shapes.
  6. Switch the PyQt6 GUI's import to the adapter; verify the
     existing GUI smoke path.
  7. Re-evaluate `headless_shirley.py` per §5; choose one of the
     three options and execute it.
  8. Mark the legacy `services/ai_service.py` as removed (or as a
     thin re-export shim during a brief deprecation window —
     implementation choice).
- **Asyncio↔Qt bridge.** `qasync` is the leading candidate; a
  background-thread asyncio loop with `QMetaObject.invokeMethod`
  for cross-thread signal delivery is the conservative
  alternative. Both are documented patterns; pick one and record
  the choice at the top of `ai_service_qt.py`.
- **`StreamEvent` schema.** Frozen dataclass or Pydantic model.
  Must cover today's signal payloads (chunk text, tool-call name
  + args, tool-call result, chart artefact base64 + caption,
  final assembled message, error text).
- **Test baseline.** Before the refactor, snapshot the existing
  test suite for `services/ai_service.py` (and
  `services/headless_shirley.py`). After the refactor, the same
  behavioural assertions pass against the new structure.
- **Qt-free regression guard for the core.** Imports
  `services.ai_service_core` in a fresh subprocess and asserts
  `"PyQt6"` is not in `sys.modules`, mirroring the guard ADR-0029
  established for `headless_shirley`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:**
  - **Maintainability** — single source of truth for the
    tool-execution loop; layering exception narrowed; CI does not
    require Qt to test the core.
  - **Modularity** — Qt coupling isolated to one adapter file.
  - **Testability** — core can be exercised in pure asyncio
    tests, without `QApplication`.
- **BAIT/VAIT — streaming auditability.** The `StreamEvent`
  shape exposes `tool_call_started` and `tool_call_completed` as
  first-class events, so an audit pipeline can attach to a
  single seam to record every model-driven action. The wire
  shape does not require a special path for either the GUI or
  the web consumer.
- **ADR-0022 (Tool Trust Classes and Gating Policy).** The
  per-turn `begin_turn` / `end_turn` brackets and the
  trust-class lock state remain in the core. ADR-0036's named
  follow-up (per-role overlay on tool classes) finds a single
  attachment point in the refactored core.

## References

- ADR-0001 (Layered Architecture and Strict One-Way
  Dependencies) — baseline.
- ADR-0010 (AIService Singleton, OpenAI-Compatible Endpoints) —
  the singleton shape preserved here.
- ADR-0011 (Acknowledged PyQt6 Dependency in AIService) — the
  follow-up this ADR fulfils.
- ADR-0012 (ToolRegistry as Single Seam) — preserved unchanged
  in the refactored core.
- ADR-0022 (Tool Trust Classes and Gating Policy) — gating
  remains in the core.
- ADR-0029 (Headless Shirley as Qt-Free Synchronous Entry
  Point) — the workaround this refactoring renders obsolete (or
  shrinks).
- ADR-0030 (Telegram Bot as First Headless Client) — existing
  consumer; call site simplified after the split.
- ADR-0031 (Module-Level Threading Lock as Interim Concurrency
  Control) — limitation that becomes addressable once the loop
  is unified.
- ADR-0033 (Web Migration: Architectural Shift) — Phase 1 is the
  implementation window for this refactoring.
- ADR-0036 (Authentication Strategy) — names the follow-up
  (per-role overlay on tool-trust classes) that the refactored
  core will host.
- ADR-0037 (Frontend Stack) — SSE consumer of the async
  generator.
- ADR-0039 (Migration Pattern: Strangler with Tagged Demo-Stable
  Branch) — the demo discipline this refactoring must satisfy.
- `services/ai_service.py`, `services/headless_shirley.py` — the
  files affected.

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-05-03 | PortfoliFLOW project owner   | Initial draft. Splits `services/ai_service.py` into a Qt-free `ai_service_core.py` (asyncio-based, owns the tool-execution loop and ADR-0022 gating) and a `ai_service_qt.py` adapter (re-emits async-generator events as `pyqtSignal`s). Fulfils the follow-up announced in ADR-0011 and removes the duplication ADR-0029 named. Implementation is Phase-1 work under ADR-0033 / ADR-0039. |
| 2026-05-03 | PortfoliFLOW project owner   | Stream A1 completed. `services/ai_service_core.py` and `services/ai_service_qt.py` shipped; `services/ai_service.py` retained as a thin deprecation shim (`from .ai_service_qt import AIServiceQt as AIService` / `from .ai_service_qt import get_ai_service_qt as get_ai_service`). Tool-execution loop lifted unchanged into the core (per §3) using `openai.AsyncOpenAI` with a per-call client (avoids the cross-thread `httpx.AsyncClient` hazard the per-call `QThread` lifecycle would otherwise expose). The `StreamEvent` vocabulary chosen at implementation: `chunk`, `tool_called`, `tool_completed`, `chart_artifact`, `stream_finished`, `error`. The Qt adapter spawns one `QThread` per `send_message`, runs `asyncio.run` on a short-lived coroutine that drains the core's async generator, and translates events back into the legacy signal names (`response_chunk`, `response_complete`, `tool_call_started`, `chart_generated`, `error_occurred`, `connection_status_changed`, `models_loaded`) bit-for-bit unchanged — GUI consumers required only an import-path change. Asyncio↔Qt bridge: ADR §5's "dedicated background-thread asyncio loop" alternative was chosen over `qasync` to avoid a new dependency and to mirror the legacy `_StreamWorker` lifecycle. Service-layer consumers `services/scraper/service.py` and `services/web_research/service.py` repointed at the core (`get_ai_service_core`) instead of the legacy shim, eliminating their transitive PyQt6 import. Qt-free invariant of the core enforced by `tests/regression/test_ai_service_core_qt_free.py` (fresh-subprocess guard, mirroring ADR-0029's pattern). Characterisation tests for the legacy `services/ai_service.py` were rewritten in Phase A1.2 (`tests/characterization/test_ai_service.py`, 18 tests) and re-layered in Phase A1.3 across `tests/characterization/test_ai_service_core.py` (asyncio path, 10 tests) and `tests/characterization/test_ai_service_qt.py` (signal path, 7 tests including a combined C-01..C-03 smoke). C-12 was dropped (no cancel mechanism exists in the legacy worker and the lift-the-loop-unchanged rule did not add one). The decision on `services/headless_shirley.py` (§5: thin wrapper / preserved-API reimplementation / retirement) is deferred to stream A2; the file is untouched by A1 and the bot's call site is unchanged. |
| 2026-05-03 | PortfoliFLOW project owner   | Stream A2 completed. Decision §5(c) executed: `services/headless_shirley.py` retired (file removed). The Telegram bot now consumes `AIServiceCore.stream_response` directly — `bot/telegram_bot.py` builds its own `AIServiceCore` instance from `BotSettings` (separate from the GUI singleton, so `.env`-driven and `QSettings`-driven configurations cannot collide) and the aiogram async handler awaits the core's generator without the previous `run_in_executor` bridge. The bot-side duplicate `_load_system_prompt` was deleted in favour of `AIServiceCore.get_system_prompt`. ADR-0031's `_TURN_LOCK` (a `threading.Lock`, kept thread-based rather than `asyncio.Lock` because consumers dispatch from different event loops) was relocated from `services/headless_shirley.py` to `services/ai_service_core.py` at module level, acquired around the entire turn in `stream_response`. Side effect: because every consumer now routes through this seam, the lock additionally closes the bot-vs-GUI cross-channel race that ADR-0031 had previously named as a known limitation; this is recorded in ADR-0031's revision history but does not change the "full `ToolRegistry` thread-safety fix is still bundled with ADR-0018" stance. Smoke test suite `tests/integration/test_telegram_bot_smoke.py` (T-01..T-04) was added before the migration as a behaviour pin and updated in lockstep with the migration; characterisation test C-17a was inverted (was "no lock in core" → now "lock present in core") and the lock-serialisation wall-clock test was ported from the deleted `tests/services/test_headless_shirley.py`. Full suite green: 570 tests (down from 578; the 8-test delta is the deleted `test_headless_shirley.py`, behaviourally re-covered by the ported tests and the existing characterisation suite). ADR fully realised. Decider: PortfoliFLOW project owner. |
| 2026-05-10 | PortfoliFLOW project owner   | Translated residual German passages to English per ADR-0008 (Phase-6 Block 0c). No substantive change; status, decisions, and content unchanged. |
| 2026-07-02 | PortfoliFLOW project owner   | GUI sunset (ADR-0094 Stage 1): the **Qt-adapter half** of this decision — `services/ai_service_qt.py` plus the `services/ai_service.py` deprecation shim — was removed together with the `gui/` surface, and PyQt6 (and `pytest-qt`) left the dependency tree; ADR-0011's narrowed exception is thereby superseded. The **Qt-free `AIServiceCore`** remains the live decision and the single entry point for every consumer (web SSE at `web/routes/chat.py`, the Telegram bot). This ADR stays **Accepted** for that core; only its adapter half is historical. No status change. |
| 2026-08-04 | PortfoliFLOW project owner   | Test hygiene, no decision change: the stream-A2 smoke suite `tests/integration/test_telegram_bot_smoke.py` (T-01..T-04) was retired, and the now-empty `tests/integration/` package with it. It was the behaviour pin *for* the `headless_shirley` → `ai_service_core` migration recorded in the 2026-05-03 stream-A2 entry above; that migration is long complete and the `tests/bot` package pins the same observable behaviour against the current multiplexed, pairing-based bot (ADR-0112 §5). Each of T-01..T-04 was checked against a named modern equivalent before deletion; the one assertion the modern suite did not carry — the fallback error reply sent when `stream_response` raises — was ported into `tests/bot/test_conversation_memory.py` M-04. The 2026-05-03 entry stands as written: it records what was true then. |
